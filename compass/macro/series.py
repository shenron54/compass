"""Series catalogue and the derived series the dashboard and rules read.

Keys look like FRED:T10Y3M, MULTPL:CAPE, IB:AMD (daily closes from IBKR) or a derived name
such as NDX_ABOVE_200. Every rule and indicator reads through get_series(), so the history view
replays rules on exactly the numbers the live check uses.
"""
import bisect
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta

from compass.db import load_series

Points = list[tuple[str, float]]

FRED_SERIES = {
    "DFF": "Effective federal funds rate",
    "DGS10": "10-year Treasury yield",
    "DGS2": "2-year Treasury yield",
    "T10Y2Y": "10-year minus 2-year Treasury spread",
    "T10Y3M": "10-year minus 3-month Treasury spread",
    "BAMLH0A0HYM2": "US high-yield option-adjusted spread",
    "NFCI": "Chicago Fed National Financial Conditions Index",
    "VIXCLS": "CBOE VIX",
    "SAHMREALTIME": "Sahm rule recession indicator (real time)",
    "ICSA": "Initial jobless claims",
    "UNRATE": "Unemployment rate",
    "CPIAUCSL": "Consumer price index (all urban)",
    "M2SL": "M2 money stock",
    "NASDAQ100": "Nasdaq-100 index",
    "SP500": "S&P 500 index (last 10 years on FRED)",
    "DEXHKUS": "HKD per USD",
    "DEXUSUK": "USD per GBP",
}
MULTPL_SERIES = {"CAPE": "Shiller CAPE (monthly)", "SP500": "S&P 500 (monthly, since 1871)"}


def pct_above_ma(points: Points, window: int = 200) -> Points:
    out, total = [], 0.0
    for i, (d, v) in enumerate(points):
        total += v
        if i >= window:
            total -= points[i - window][1]
        if i >= window - 1:
            out.append((d, (v / (total / window) - 1) * 100))
    return out


def moving_average(points: Points, window: int) -> Points:
    out, total = [], 0.0
    for i, (d, v) in enumerate(points):
        total += v
        if i >= window:
            total -= points[i - window][1]
        if i >= window - 1:
            out.append((d, total / window))
    return out


def drawdown(points: Points) -> Points:
    out, peak = [], float("-inf")
    for d, v in points:
        peak = max(peak, v)
        out.append((d, (v / peak - 1) * 100))
    return out


def rsi(points: Points, period: int = 14) -> Points:
    if len(points) <= period:
        return []
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = points[i][1] - points[i - 1][1]
        gains += max(change, 0)
        losses += max(-change, 0)
    avg_gain, avg_loss = gains / period, losses / period
    out = []
    for i in range(period, len(points)):
        if i > period:
            change = points[i][1] - points[i - 1][1]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        value = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        out.append((points[i][0], value))
    return out


def yoy(points: Points, periods: int = 12) -> Points:
    return [(points[i][0], (points[i][1] / points[i - periods][1] - 1) * 100)
            for i in range(periods, len(points)) if points[i - periods][1]]


def expanding_percentile(points: Points, min_history: int = 120) -> Points:
    """Percentile of each value among all values before it: no look-ahead, so replays are fair."""
    seen: list[float] = []
    out = []
    for d, v in points:
        if len(seen) >= min_history:
            out.append((d, bisect.bisect_left(seen, v) / len(seen) * 100))
        bisect.insort(seen, v)
    return out


def percentile_of(value: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    ordered = sorted(history)
    return bisect.bisect_left(ordered, value) / len(ordered) * 100


def value_on_or_before(points: Points, day: str) -> tuple[str, float] | None:
    i = bisect.bisect_right(points, (day, float("inf")))
    return points[i - 1] if i else None


def days_before(day: str, days: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()


DERIVED: dict[str, tuple[str, Callable[[Points], Points], str]] = {
    "CAPE_PCT": ("MULTPL:CAPE", lambda p: expanding_percentile(p, 120),
                 "Shiller CAPE percentile vs all earlier months"),
    "NDX_ABOVE_200": ("FRED:NASDAQ100", pct_above_ma, "Nasdaq-100 % above its 200-day average"),
    "SPX_ABOVE_200": ("FRED:SP500", pct_above_ma, "S&P 500 % above its 200-day average"),
    "NDX_DD": ("FRED:NASDAQ100", drawdown, "Nasdaq-100 % below its all-time high"),
    "SPX_DD": ("FRED:SP500", drawdown, "S&P 500 % below its 10-year high"),
    "SPX_LONG_DD": ("MULTPL:SP500", drawdown, "S&P 500 % below its high (monthly, since 1871)"),
    "NDX_RSI": ("FRED:NASDAQ100", rsi, "Nasdaq-100 14-day RSI"),
    "CPI_YOY": ("FRED:CPIAUCSL", yoy, "CPI inflation, % year on year"),
    "M2_YOY": ("FRED:M2SL", yoy, "M2 money supply, % year on year"),
    "ICSA_4W": ("FRED:ICSA", lambda p: [(d, v / 1000) for d, v in moving_average(p, 4)],
                "Initial jobless claims, 4-week average (thousands)"),
}


def describe(key: str) -> str:
    if key in DERIVED:
        return DERIVED[key][2]
    source, _, name = key.partition(":")
    if source == "FRED":
        return FRED_SERIES.get(name, name)
    if source == "MULTPL":
        return MULTPL_SERIES.get(name, name)
    if source == "IB":
        return f"{name} daily close (IBKR)"
    return key


def get_series(conn: sqlite3.Connection, key: str, start: str | None = None) -> Points:
    if key in DERIVED:
        base, fn, _ = DERIVED[key]
        points = fn(load_series(conn, base))
        return [p for p in points if p[0] >= start] if start else points
    return load_series(conn, key, start)


def downsample(points: Points, max_points: int = 400) -> Points:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = [points[int(i * step)] for i in range(max_points)]
    if picked[-1] != points[-1]:
        picked.append(points[-1])
    return picked
