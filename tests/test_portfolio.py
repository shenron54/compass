from types import SimpleNamespace as NS

import pytest

from compass.ibkr.gateway import GatewayError, parse_account
from compass.portfolio.config import PortfolioConfig, load_lookthrough
from compass.portfolio.history import (
    nav_series,
    net_contributions,
    prune_intraday,
    store_account,
    store_flex,
)
from compass.portfolio.view import build_view, load_view

DBL_MAX = 1.7976931348623157e308


def _values(base="HKD"):
    rows = [("NetLiquidation", base, "100000"), ("TotalCashValue", base, "10000"),
            ("UnrealizedPnL", "BASE", "1500"), ("RealizedPnL", "BASE", str(DBL_MAX)),
            ("ExchangeRate", "USD", "7.8"), ("ExchangeRate", "GBP", "10"),
            ("ExchangeRate", "BASE", "1")]
    return [NS(account="U1", tag=t, currency=c, value=v) for t, c, v in rows]


def _item(symbol, ccy, qty, mv, price=1.0):
    return NS(account="U1", position=qty, marketPrice=price, marketValue=mv, averageCost=1.0,
              unrealizedPNL=10.0, realizedPNL=0.0,
              contract=NS(symbol=symbol, conId=hash(symbol) % 1000, primaryExchange="LSE",
                          exchange="", currency=ccy, secType="STK"))


def test_parse_account_converts_to_base_with_ib_rates():
    state = parse_account(_values(), [_item("EQQQ", "GBP", 10, 4500), _item("GOOG", "USD", 5, 1000)])
    assert state.base_currency == "HKD"
    assert state.nav_base == 100000 and state.cash_base == 10000
    eqqq = next(p for p in state.positions if p.symbol == "EQQQ")
    assert eqqq.market_value_base == 45000
    assert state.realized_pnl_base is None


def test_parse_account_skips_closed_positions():
    state = parse_account(_values(), [_item("AMD", "USD", 0, 0)])
    assert state.positions == []


def test_parse_account_needs_a_rate_for_every_currency():
    with pytest.raises(GatewayError, match="JPY"):
        parse_account(_values(), [_item("7203", "JPY", 100, 300000)])


def test_parse_account_filters_by_account():
    with pytest.raises(GatewayError, match="no account values"):
        parse_account(_values(), [], account="U9")


def _cfg(**kw):
    return PortfolioConfig(targets={"EQQQ": 0.5, "GOOG": 0.1, "CASH": 0.1},
                           bands={"default": 0.05}, **kw)


def _snapshot(nav=100000.0, cash=10000.0):
    return {"ts": "2026-11-10T00:00:00+00:00", "source": "ibkr", "account": "U1",
            "base_currency": "HKD", "nav_base": nav, "cash_base": cash,
            "unrealized_pnl_base": 0.0}


def _pos(symbol, value_base, ccy="USD", fx=7.8):
    return {"symbol": symbol, "currency": ccy, "quantity": 1, "market_price": value_base / fx,
            "avg_cost": None, "market_value": value_base / fx, "fx_to_base": fx,
            "market_value_base": value_base, "unrealized_pnl": None, "con_id": 1}


def test_look_through_adds_fund_share_to_direct_holding(settings):
    lt = load_lookthrough(settings.local_config)
    view = build_view(_snapshot(), [_pos("EQQQ", 60000, "GBP", 10), _pos("GOOG", 30000)],
                      _cfg(), lt)
    alphabet = next(e for e in view.names if e.key == "GOOG")
    assert alphabet.direct == pytest.approx(0.30)
    assert alphabet.via_funds == pytest.approx(0.60 * lt.etfs["EQQQ"].names["GOOG"])
    assert view.lookthrough_as_of == {"EQQQ": lt.etfs["EQQQ"].as_of}


def test_tech_weight_counts_chosen_sectors_inside_funds(settings):
    lt = load_lookthrough(settings.local_config)
    view = build_view(_snapshot(), [_pos("EQQQ", 60000, "GBP", 10), _pos("GOOG", 30000)],
                      _cfg(), lt)
    s = lt.etfs["EQQQ"].sectors
    expected = 0.6 * (s["Information Technology"] + s["Communication Services"]) + 0.3
    assert view.tech_weight == pytest.approx(expected)


def test_band_status_marks_over_and_under(settings):
    lt = load_lookthrough(settings.local_config)
    view = build_view(_snapshot(), [_pos("EQQQ", 60000, "GBP", 10), _pos("GOOG", 30000)],
                      _cfg(), lt)
    status = {h.symbol: h.status for h in view.holdings}
    assert status == {"EQQQ": "over", "GOOG": "over"}
    assert view.cash_status == "ok"


def test_unknown_symbol_is_unclassified(settings):
    view = build_view(_snapshot(), [_pos("XYZ", 50000)], _cfg(),
                      load_lookthrough(settings.local_config))
    assert dict(view.sectors)["Unclassified"] == pytest.approx(0.5)
    assert view.holdings[0].status == "untargeted"


def test_flex_nav_wins_over_snapshots_on_the_same_day(conn, settings):
    state = parse_account(_values(), [_item("GOOG", "USD", 5, 1000)])
    state.ts = "2026-11-06T09:00:00+00:00"
    store_account(conn, state)
    report = NS(trades=[], cash=[], nav=[{"date": "2026-11-06", "total_base": 99000.0,
                                          "cash_base": None, "stock_base": None}])
    store_flex(conn, report)
    assert nav_series(conn, "Asia/Hong_Kong") == [("2026-11-06", 99000.0, "flex")]


def test_net_contributions_accumulate_deposits_only(conn):
    report = NS(trades=[], nav=[], cash=[
        {"transaction_id": "1", "date": "2026-01-01", "type": "Deposits/Withdrawals",
         "currency": "HKD", "amount": 1000.0, "fx_to_base": 1.0, "symbol": None,
         "description": ""},
        {"transaction_id": "2", "date": "2026-02-01", "type": "Dividends", "currency": "USD",
         "amount": 5.0, "fx_to_base": 7.8, "symbol": "GOOG", "description": ""},
        {"transaction_id": "3", "date": "2026-03-01", "type": "Deposits/Withdrawals",
         "currency": "USD", "amount": 100.0, "fx_to_base": 7.8, "symbol": None,
         "description": ""}])
    store_flex(conn, report)
    assert net_contributions(conn) == [("2026-01-01", 1000.0), ("2026-03-01", 1780.0)]


def test_prune_keeps_one_snapshot_per_old_day(conn):
    state = parse_account(_values(), [])
    for hour in ("01", "02", "03"):
        state.ts = f"2026-01-05T{hour}:00:00+00:00"
        store_account(conn, state)
    assert prune_intraday(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_load_view_returns_latest_snapshot(conn, settings):
    state = parse_account(_values(), [_item("GOOG", "USD", 5, 1000)])
    store_account(conn, state)
    view = load_view(conn, _cfg(), load_lookthrough(settings.local_config))
    assert view.nav == 100000 and view.holdings[0].symbol == "GOOG"
