import pytest
from pydantic import ValidationError

from compass.portfolio.config import PortfolioConfig, load_lookthrough
from compass.portfolio.view import build_view
from compass.rules.engine import Context, evaluate, freeze_states, ladder_states
from compass.rules.models import parse_rule, parse_rule_yaml
from compass.rules.replay import forward_return, replay
from compass.rules.store import active_alerts, evaluate_all, save_rule
from tests.conftest import daily, put

CFG = PortfolioConfig(targets={"EQQQ": 0.4, "AMD": 0.1, "CASH": 0.1},
                      price_series={"EQQQ": "FRED:NASDAQ100"})


def _view(settings, cash=20000.0, amd=20000.0):
    snap = {"ts": "2026-11-10T00:00:00+00:00", "source": "ibkr", "account": "U1",
            "base_currency": "HKD", "nav_base": 100000.0, "cash_base": cash,
            "unrealized_pnl_base": 0.0}
    pos = [{"symbol": "EQQQ", "currency": "GBP", "quantity": 1, "market_price": 1,
            "avg_cost": 1, "market_value": 1, "fx_to_base": 10,
            "market_value_base": 100000.0 - cash - amd},
           {"symbol": "AMD", "currency": "USD", "quantity": 1, "market_price": 1, "avg_cost": 1,
            "market_value": 1, "fx_to_base": 7.8, "market_value_base": amd}]
    return build_view(snap, pos, CFG, load_lookthrough(settings.local_config))


def test_allocation_band_fires_above_max_with_amount(conn, settings):
    rule = parse_rule({"id": "amd-cap", "name": "AMD cap", "type": "allocation_band",
                       "measure": "position_weight", "symbols": ["AMD"], "max": 0.15})
    ev = evaluate(rule, Context(conn, _view(settings), CFG))
    assert ev.fired and ev.state_key == "over"
    assert "5,000 HKD" in ev.suggestions[0]


def test_allocation_band_quiet_inside_band(conn, settings):
    rule = parse_rule({"id": "cash", "name": "Cash", "type": "allocation_band",
                       "measure": "cash_weight", "min": 0.05, "max": 0.3})
    assert not evaluate(rule, Context(conn, _view(settings), CFG)).fired


def test_allocation_band_without_snapshot_reports_error(conn):
    rule = parse_rule({"id": "cash", "name": "Cash", "type": "allocation_band",
                       "measure": "cash_weight", "min": 0.05})
    ev = evaluate(rule, Context(conn, None, CFG))
    assert not ev.fired and ev.error == "no portfolio snapshot yet"


def test_freeze_has_hysteresis():
    states = freeze_states(daily([80, 96, 90, 86, 84, 96]), above=95, resume_below=85)
    assert [s for _, s in states] == [False, True, True, True, False, True]


def test_freeze_rule_reports_since_date(conn):
    put(conn, "FRED:VIXCLS", daily([20, 40, 35, 31], start="2026-01-01"))
    rule = parse_rule({"id": "vix-freeze", "name": "VIX", "type": "valuation_freeze",
                       "series": "FRED:VIXCLS", "above": 38, "resume_below": 30})
    ev = evaluate(rule, Context(conn, None, CFG))
    assert ev.fired and ev.inputs["since"] == "2026-01-02"


def test_ladder_levels_reset_near_high():
    steps = ladder_states(daily([100, 89, 79, 85, 98, 99]), [0.1, 0.2], reset_within=0.03)
    assert [s.level for s in steps] == [-1, 0, 1, 1, -1, -1]
    assert steps[2].cycle_start == steps[1].date


def test_ladder_suggests_tranche_amounts_from_cash(conn, settings):
    put(conn, "FRED:NASDAQ100", daily([100, 85, 78]))
    rule = parse_rule({"id": "ladder", "name": "Ladder", "type": "drawdown_ladder",
                       "cash_floor": 4000, "tranches": [
                           {"drawdown": 0.2, "fraction": 0.5, "buy": "EQQQ"},
                           {"drawdown": 0.1, "fraction": 0.25, "buy": "VUAA"}]})
    ev = evaluate(rule, Context(conn, _view(settings, cash=20000), CFG))
    assert ev.fired and ev.state_key.endswith(":t2")
    assert "4,000 HKD into VUAA" in ev.suggestions[0]
    assert "8,000 HKD into EQQQ" in ev.suggestions[1]


def test_trim_needs_both_extension_and_overweight(conn, settings):
    put(conn, "FRED:NASDAQ100", daily([100] * 199 + [140]))
    rule = parse_rule({"id": "trim", "name": "Trim AMD", "type": "trim_at_high",
                       "symbol": "AMD", "series": "FRED:NASDAQ100", "above_200dma": 0.25,
                       "overweight_by": 0.05})
    assert evaluate(rule, Context(conn, _view(settings, amd=20000), CFG)).fired
    assert not evaluate(rule, Context(conn, _view(settings, amd=12000), CFG)).fired


def test_rule_validation_rejects_bad_tranches():
    with pytest.raises(ValidationError, match="more than 1"):
        parse_rule({"id": "x", "name": "x", "type": "drawdown_ladder", "tranches": [
            {"drawdown": 0.1, "fraction": 0.7}, {"drawdown": 0.2, "fraction": 0.7}]})


def test_rule_validation_rejects_inverted_freeze():
    with pytest.raises(ValidationError, match="resume_below"):
        parse_rule({"id": "x", "name": "x", "type": "valuation_freeze", "above": 80,
                    "resume_below": 90})


def test_rule_yaml_must_be_a_mapping():
    with pytest.raises(TypeError):
        parse_rule_yaml("- just a list")


def test_events_recorded_only_on_state_change(conn, settings):
    save_rule(conn, "id: cash\nname: Cash floor\ntype: allocation_band\nmeasure: cash_weight\n"
                    "min: 0.3\n")
    view = _view(settings)
    evaluate_all(conn, view, CFG)
    evaluate_all(conn, view, CFG)
    assert conn.execute("SELECT COUNT(*) FROM rule_events").fetchone()[0] == 1
    assert active_alerts(conn)[0]["rule_id"] == "cash"
    evaluate_all(conn, _view(settings, cash=40000), CFG)
    assert active_alerts(conn) == []


def test_forward_return_uses_next_available_date():
    pts = daily([100, 110, 121])
    assert forward_return(pts, pts[0][0], 2) == pytest.approx(0.21)
    assert forward_return(pts, pts[0][0], 10) is None


def test_ladder_replay_lists_each_tranche_once_per_cycle(conn):
    put(conn, "FRED:NASDAQ100", daily([100, 89, 79, 100, 88, 100]))
    rule = parse_rule({"id": "l", "name": "L", "type": "drawdown_ladder", "tranches": [
        {"drawdown": 0.1, "fraction": 0.5}, {"drawdown": 0.2, "fraction": 0.5}]})
    rp = replay(conn, rule, CFG)
    assert [row[1].split()[0] for row in rp.rows] == ["T1", "T2", "T1"]
    assert rp.summary[0].startswith("2 drawdown cycles")


def test_freeze_replay_finds_episodes(conn):
    put(conn, "FRED:VIXCLS", daily([20, 40, 25, 45, 46]))
    put(conn, "FRED:SP500", daily([100, 90, 95, 80, 85]))
    rule = parse_rule({"id": "f", "name": "F", "type": "valuation_freeze", "series": "FRED:VIXCLS",
                       "above": 38, "resume_below": 30, "price_series": "FRED:SP500"})
    rp = replay(conn, rule, CFG)
    assert [row[1] for row in rp.rows] == [daily([0, 0, 0])[2][0], "still on"]
