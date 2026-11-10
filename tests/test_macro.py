from datetime import date

import pytest

from compass.macro import indicators as ind
from compass.macro.series import (
    drawdown,
    expanding_percentile,
    get_series,
    pct_above_ma,
    percentile_of,
    rsi,
    yoy,
)
from tests.conftest import daily, put


def test_pct_above_ma_starts_after_the_window():
    pts = daily([100] * 199 + [120])
    out = pct_above_ma(pts, 200)
    assert len(out) == 1
    assert out[0][1] == pytest.approx((120 / (100.1) - 1) * 100)


def test_drawdown_tracks_running_peak():
    assert [round(v) for _, v in drawdown(daily([100, 120, 90, 130]))] == [0, 0, -25, 0]


def test_rsi_is_100_when_price_only_rises():
    assert rsi(daily(range(1, 30)))[-1][1] == 100.0


def test_rsi_stays_between_0_and_100():
    values = [100 + (i % 7) * 3 - (i % 5) * 4 for i in range(80)]
    assert all(0 <= v <= 100 for _, v in rsi(daily(values)))


def test_yoy_compares_twelve_periods_back():
    assert yoy(daily([100] * 12 + [103]))[-1][1] == pytest.approx(3.0)


def test_expanding_percentile_never_looks_ahead():
    out = expanding_percentile(daily([1, 2, 3, 0]), min_history=2)
    assert [round(v) for _, v in out] == [100, 0]


def test_percentile_of_needs_history():
    assert percentile_of(5, [1]) is None
    assert percentile_of(5, [1, 2, 3, 10]) == 75


def test_derived_series_read_from_their_base(conn):
    put(conn, "FRED:NASDAQ100", daily([100, 80]))
    assert get_series(conn, "NDX_DD")[-1][1] == pytest.approx(-20)


def _read(conn, ind_id, points, key=None):
    i = ind.BY_ID[ind_id]
    put(conn, key or i.series, points)
    return ind.read(conn, i, today=date.fromisoformat(points[-1][0]))


def test_inverted_curve_is_caution(conn):
    r = _read(conn, "curve_10y3m", daily([0.5] * 10 + [-0.4]))
    assert (r.tone, r.label) == ("caution", "Inverted")


def test_curve_back_above_zero_after_inversion_is_stress(conn):
    r = _read(conn, "curve_10y3m", daily([-0.5] * 100 + [0.2]))
    assert r.tone == "stress" and "after inversion" in r.label


def test_sahm_trigger(conn):
    r = _read(conn, "sahm", daily([0.1, 0.55], start="2026-09-01"))
    assert r.label == "Triggered"


def test_stale_reading_flagged(conn):
    i = ind.BY_ID["vix"]
    put(conn, i.series, daily([18], start="2026-01-01"))
    r = ind.read(conn, i, today=date(2026, 3, 1))
    assert r.stale


def test_missing_series_reads_as_no_data(conn):
    r = ind.read(conn, ind.BY_ID["hy_spread"])
    assert not r.available and r.label == "No data"


def test_regime_stressed_with_two_stress_signals():
    readings = [ind.Reading("sahm", "Sahm", "Labour", "pp", "x", "l", value=0.6, tone="stress"),
                ind.Reading("vix", "VIX", "Vol", "index", "x", "l", value=35, tone="stress")]
    conditions = ind.regime(readings)[0]
    assert conditions.label == "Stressed"
    assert len(conditions.reasons) == 2


def test_regime_valuation_stretched_at_high_cape_percentile():
    readings = [ind.Reading("cape", "CAPE", "Valuation", "x", "x", "l", value=40,
                            percentile=97, tone="stress")]
    assert ind.regime(readings)[1].label == "Stretched"
