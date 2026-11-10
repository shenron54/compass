"""Indicator readings: the latest value, where it sits in its own history, and a plain label.

Thresholds are conventional rules of thumb, written out so the explanation can quote them. They
describe conditions; none of them forecasts a turn on its own.
"""
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from compass.macro.series import (
    Points,
    days_before,
    describe,
    downsample,
    get_series,
    percentile_of,
    value_on_or_before,
)

CALM, CAUTION, STRESS = "calm", "caution", "stress"
STALE_DAYS = {"daily": 7, "weekly": 21, "monthly": 75}


@dataclass
class Reading:
    id: str
    name: str
    group: str
    unit: str
    series: str
    lesson: str
    date: str | None = None
    value: float | None = None
    change: float | None = None
    change_label: str = ""
    percentile: float | None = None
    tone: str = CALM
    label: str = "No data"
    text: str = ""
    stale: bool = False
    history_start: str | None = None
    spark: Points = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.value is not None


Status = Callable[[float, Points, float | None], tuple[str, str, str]]


@dataclass(frozen=True)
class Indicator:
    id: str
    name: str
    group: str
    series: str
    unit: str
    freq: str
    lesson: str
    status: Status
    change_days: int = 30
    change_label: str = "1 month"


def _fmt(v: float, unit: str) -> str:
    if unit == "%":
        return f"{v:.2f}%"
    if unit == "pp":
        return f"{v:+.2f} pp"
    if unit == "k":
        return f"{v:,.0f}k"
    return f"{v:,.2f}"


def _curve(v: float, pts: Points, _pct: float | None) -> tuple[str, str, str]:
    last_date = pts[-1][0]
    recent = [x for d, x in pts if d >= days_before(last_date, 365)]
    if v < 0:
        return CAUTION, "Inverted", (
            f"Short rates are above long rates by {abs(v):.2f} pp. Inversions have preceded "
            "every US recession since the late 1960s, with a lead of roughly 6 to 24 months, "
            "and there have also been long inversions with no recession after them.")
    if recent and min(recent) < 0:
        return STRESS, "Re-steepening after inversion", (
            f"The spread is back above zero ({v:+.2f} pp) after being inverted in the last "
            "12 months. Historically the un-inversion, not the inversion, has come closest to "
            "the start of recessions.")
    return CALM, "Normal (upward sloping)", f"Long rates exceed short rates by {v:.2f} pp."


def _hy(v: float, pts: Points, _pct: float | None) -> tuple[str, str, str]:
    past = value_on_or_before(pts, days_before(pts[-1][0], 90))
    widening = past is not None and v - past[1] >= 1.5
    if v >= 7:
        return STRESS, "Credit stress", f"High-yield spreads at {v:.2f}% price real default fear."
    if v >= 5 or widening:
        why = "widened by 1.5 pp or more in 3 months" if widening else f"are {v:.2f}%"
        return CAUTION, "Widening", f"High-yield spreads {why}; lenders are asking for more."
    if v < 3.5:
        return CALM, "Tight", (f"Spreads at {v:.2f}% are tight: credit is easy, and investors "
                               "are paid little for default risk (complacency is possible).")
    return CALM, "Normal", f"High-yield spreads at {v:.2f}% are near their long-run range."


def _nfci(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v > 0.5:
        return STRESS, "Tight", f"NFCI {v:+.2f}: financial conditions are much tighter than average."
    if v > 0:
        return CAUTION, "Tighter than average", f"NFCI {v:+.2f}: above zero means tighter than average."
    if v < -0.3:
        return CALM, "Loose", f"NFCI {v:+.2f}: money and credit are easy to get."
    return CALM, "Near average", f"NFCI {v:+.2f}: zero is the long-run average."


def _vix(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v >= 30:
        return STRESS, "Fear", f"VIX {v:.1f}: options price large daily swings."
    if v >= 20:
        return CAUTION, "Elevated", f"VIX {v:.1f}: above its long-run average of about 19-20."
    if v < 15:
        return CALM, "Complacent", f"VIX {v:.1f}: markets expect little movement."
    return CALM, "Normal", f"VIX {v:.1f} is near its usual range."


def _sahm(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v >= 0.5:
        return STRESS, "Triggered", (f"{v:.2f} pp: the 3-month average unemployment rate is 0.5 "
                                     "pp or more above its 12-month low, the recession signal.")
    if v >= 0.3:
        return CAUTION, "Rising", f"{v:.2f} pp: unemployment is drifting up; 0.50 is the trigger."
    return CALM, "Not triggered", f"{v:.2f} pp, below the 0.50 trigger."


def _claims(v: float, pts: Points, _pct: float | None) -> tuple[str, str, str]:
    year = [x for d, x in pts if d >= days_before(pts[-1][0], 365)]
    low = min(year) if year else v
    rise = (v / low - 1) * 100 if low else 0
    if rise >= 30:
        return STRESS, "Rising sharply", f"{v:,.0f}k, {rise:.0f}% above the 52-week low."
    if rise >= 15:
        return CAUTION, "Rising", f"{v:,.0f}k, {rise:.0f}% above the 52-week low."
    return CALM, "Low and stable", f"{v:,.0f}k, {rise:.0f}% above the 52-week low."


def _unrate(v: float, pts: Points, _pct: float | None) -> tuple[str, str, str]:
    year = [x for d, x in pts if d >= days_before(pts[-1][0], 365)]
    low = min(year) if year else v
    if v - low >= 0.5:
        return CAUTION, "Rising", f"{v:.1f}%, {v - low:.1f} pp above its 12-month low."
    return CALM, "Steady", f"{v:.1f}%, {v - low:.1f} pp above its 12-month low."


def _cpi(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v >= 4:
        return STRESS, "High", f"Inflation {v:.1f}% limits how far the Fed can cut."
    if v >= 2.5:
        return CAUTION, "Above target", f"Inflation {v:.1f}% is above the Fed's 2% goal."
    return CALM, "Near target", f"Inflation {v:.1f}% is close to the Fed's 2% goal."


def _m2(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v < 0:
        return CAUTION, "Shrinking", f"M2 is {v:.1f}% lower than a year ago, which is rare."
    return CALM, "Growing", f"M2 is {v:.1f}% higher than a year ago."


def _fed(v: float, pts: Points, _pct: float | None) -> tuple[str, str, str]:
    past = value_on_or_before(pts, days_before(pts[-1][0], 182))
    if past is None:
        return CALM, "Unknown trend", f"Fed funds at {v:.2f}%."
    change = v - past[1]
    if change <= -0.2:
        return CALM, "Cutting", f"{v:.2f}%, down {abs(change):.2f} pp in 6 months."
    if change >= 0.2:
        return CAUTION, "Hiking", f"{v:.2f}%, up {change:.2f} pp in 6 months."
    return CALM, "On hold", f"{v:.2f}%, little changed over 6 months."


def _cape(v: float, _p: Points, pct: float | None) -> tuple[str, str, str]:
    where = f"higher than {pct:.0f}% of months since 1881" if pct is not None else ""
    if pct is not None and pct >= 90:
        return STRESS, "Stretched", (f"CAPE {v:.1f}, {where}. High CAPE has gone with lower "
                                     "10-year returns, but says little about the next year.")
    if pct is not None and pct >= 70:
        return CAUTION, "Elevated", f"CAPE {v:.1f}, {where}."
    if pct is not None and pct <= 30:
        return CALM, "Cheap", f"CAPE {v:.1f}, {where}."
    return CALM, "Fair", f"CAPE {v:.1f}, {where}. The long-run average is about 17."


def _trend(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v >= 15:
        return CAUTION, "Extended", f"{v:.1f}% above the 200-day average: stretched above trend."
    if v >= 0:
        return CALM, "Uptrend", f"{v:.1f}% above the 200-day average."
    if v > -10:
        return CAUTION, "Below trend", f"{abs(v):.1f}% below the 200-day average."
    return STRESS, "Downtrend", f"{abs(v):.1f}% below the 200-day average."


def _drawdown(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v <= -20:
        return STRESS, "Bear market", f"{abs(v):.1f}% below the high (a bear market is -20%)."
    if v <= -10:
        return CAUTION, "Correction", f"{abs(v):.1f}% below the high (a correction is -10%)."
    if v <= -5:
        return CALM, "Pullback", f"{abs(v):.1f}% below the high."
    return CALM, "Near high", f"{abs(v):.1f}% below the high."


def _rsi(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    if v >= 70:
        return CAUTION, "Overbought", f"RSI {v:.0f}: a strong, fast run; often pauses follow."
    if v <= 30:
        return CAUTION, "Oversold", f"RSI {v:.0f}: a sharp fall; bounces often follow."
    return CALM, "Neutral", f"RSI {v:.0f}, between 30 and 70."


def _hkd(v: float, _p: Points, _pct: float | None) -> tuple[str, str, str]:
    return CALM, "Pegged", (f"{v:.4f} HKD per USD. The HKMA holds it between 7.75 and 7.85, so "
                            "USD assets carry almost no currency risk for an HKD investor.")


INDICATORS: list[Indicator] = [
    Indicator("fed_funds", "Fed funds rate", "Rates", "FRED:DFF", "%", "daily",
              "fed-and-rates", _fed),
    Indicator("curve_10y3m", "Yield curve 10y-3m", "Rates", "FRED:T10Y3M", "pp", "daily",
              "yield-curve", _curve),
    Indicator("curve_10y2y", "Yield curve 10y-2y", "Rates", "FRED:T10Y2Y", "pp", "daily",
              "yield-curve", _curve),
    Indicator("hy_spread", "High-yield credit spread", "Credit", "FRED:BAMLH0A0HYM2", "%",
              "daily", "credit-spreads", _hy),
    Indicator("nfci", "Financial conditions (NFCI)", "Credit", "FRED:NFCI", "index", "weekly",
              "credit-spreads", _nfci),
    Indicator("vix", "VIX", "Volatility", "FRED:VIXCLS", "index", "daily",
              "volatility-vix", _vix),
    Indicator("sahm", "Sahm rule", "Labour", "FRED:SAHMREALTIME", "pp", "monthly",
              "labour-and-sahm", _sahm, 31, "1 month"),
    Indicator("claims", "Jobless claims (4-wk avg)", "Labour", "ICSA_4W", "k", "weekly",
              "labour-and-sahm", _claims),
    Indicator("unemployment", "Unemployment rate", "Labour", "FRED:UNRATE", "%", "monthly",
              "labour-and-sahm", _unrate, 31),
    Indicator("cpi_yoy", "CPI inflation (YoY)", "Inflation", "CPI_YOY", "%", "monthly",
              "inflation", _cpi, 31),
    Indicator("m2_yoy", "M2 money supply (YoY)", "Inflation", "M2_YOY", "%", "monthly",
              "inflation", _m2, 31),
    Indicator("cape", "Shiller CAPE", "Valuation", "MULTPL:CAPE", "x", "monthly",
              "valuation-cape", _cape, 365, "1 year"),
    Indicator("ndx_trend", "Nasdaq-100 vs 200-day", "Trend", "NDX_ABOVE_200", "%", "daily",
              "trend-and-200dma", _trend),
    Indicator("spx_trend", "S&P 500 vs 200-day", "Trend", "SPX_ABOVE_200", "%", "daily",
              "trend-and-200dma", _trend),
    Indicator("ndx_drawdown", "Nasdaq-100 drawdown", "Trend", "NDX_DD", "%", "daily",
              "drawdowns-and-recovery", _drawdown),
    Indicator("spx_drawdown", "S&P 500 drawdown (10y)", "Trend", "SPX_DD", "%", "daily",
              "drawdowns-and-recovery", _drawdown),
    Indicator("ndx_rsi", "Nasdaq-100 RSI (14d)", "Trend", "NDX_RSI", "index", "daily",
              "trend-and-200dma", _rsi),
    Indicator("usdhkd", "USD/HKD", "Currency", "FRED:DEXHKUS", "x", "daily",
              "ucits-and-hk-tax", _hkd),
]
BY_ID = {i.id: i for i in INDICATORS}


def read(conn: sqlite3.Connection, ind: Indicator, today: date | None = None) -> Reading:
    today = today or date.today()
    pts = get_series(conn, ind.series)
    r = Reading(ind.id, ind.name, ind.group, ind.unit, ind.series, ind.lesson)
    r.text = describe(ind.series)
    if not pts:
        return r
    r.date, r.value = pts[-1]
    r.history_start = pts[0][0]
    past = value_on_or_before(pts, days_before(r.date, ind.change_days))
    if past is not None:
        r.change = r.value - past[1]
        r.change_label = ind.change_label
    r.percentile = percentile_of(r.value, [v for _, v in pts])
    r.tone, r.label, r.text = ind.status(r.value, pts, r.percentile)
    r.stale = (today - date.fromisoformat(r.date)).days > STALE_DAYS[ind.freq]
    r.spark = downsample([p for p in pts if p[0] >= days_before(r.date, 3 * 365)], 160)
    return r


def read_all(conn: sqlite3.Connection, today: date | None = None) -> list[Reading]:
    return [read(conn, ind, today) for ind in INDICATORS]


def fmt_value(r: Reading) -> str:
    return "—" if r.value is None else _fmt(r.value, r.unit)


@dataclass
class Axis:
    name: str
    label: str
    tone: str
    reasons: list[str]


CONDITION_IDS = ("curve_10y3m", "hy_spread", "nfci", "vix", "sahm", "claims", "cpi_yoy")


def regime(readings: list[Reading]) -> list[Axis]:
    by = {r.id: r for r in readings if r.available}

    cond = [by[i] for i in CONDITION_IDS if i in by]
    stress = [r for r in cond if r.tone == STRESS]
    caution = [r for r in cond if r.tone == CAUTION]
    if len(stress) >= 2:
        c_label, c_tone = "Stressed", STRESS
    elif stress or len(caution) >= 3:
        c_label, c_tone = "Caution", CAUTION
    else:
        c_label, c_tone = ("Calm", CALM) if cond else ("No data", CALM)
    c_reasons = [f"{r.name}: {r.label}" for r in stress + caution] or [
        f"{len(cond)} of {len(CONDITION_IDS)} macro signals calm"]

    cape, ndx = by.get("cape"), by.get("ndx_trend")
    v_reasons = []
    if cape and cape.percentile is not None:
        v_reasons.append(f"CAPE {cape.value:.1f} ({cape.percentile:.0f}th percentile)")
        if cape.percentile >= 90 or (cape.percentile >= 80 and ndx and ndx.value >= 15):
            v_label, v_tone = "Stretched", STRESS
        elif cape.percentile >= 70:
            v_label, v_tone = "Elevated", CAUTION
        elif cape.percentile <= 30:
            v_label, v_tone = "Cheap", CALM
        else:
            v_label, v_tone = "Fair", CALM
    else:
        v_label, v_tone = "No data", CALM
    if ndx:
        v_reasons.append(f"Nasdaq-100 {ndx.value:+.1f}% vs 200-day")

    trends = [by[i] for i in ("ndx_trend", "spx_trend") if i in by]
    above = [r for r in trends if r.value >= 0]
    dd = by.get("ndx_drawdown")
    if not trends:
        t_label, t_tone = "No data", CALM
    elif len(above) == len(trends):
        t_label, t_tone = "Uptrend", CALM
    elif not above:
        t_label, t_tone = "Downtrend", STRESS
    else:
        t_label, t_tone = "Mixed", CAUTION
    t_reasons = [f"{r.name}: {r.value:+.1f}%" for r in trends]
    if dd:
        t_reasons.append(f"Nasdaq-100 {abs(dd.value):.1f}% below its high ({dd.label})")

    return [Axis("Conditions", c_label, c_tone, c_reasons),
            Axis("Valuation", v_label, v_tone, v_reasons),
            Axis("Trend", t_label, t_tone, t_reasons)]
