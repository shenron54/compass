"""Replay a rule over history to see when it would have fired and what happened next.

Only the market half of a rule can be replayed over decades; weight conditions need the
account's own snapshots, which start when compass started recording.
"""
import bisect
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from compass.config import get_settings
from compass.macro.series import Points, downsample, get_series, pct_above_ma
from compass.portfolio.config import PortfolioConfig, load_lookthrough
from compass.portfolio.view import build_view
from compass.rules.engine import Context, freeze_states, ladder_states
from compass.rules.models import AllocationBand, DrawdownLadder, Rule, TrimAtHigh, ValuationFreeze

HORIZONS = (("1y", 365), ("3y", 3 * 365), ("5y", 5 * 365))


@dataclass
class Replay:
    series: str
    chart: Points
    markers: list[dict] = field(default_factory=list)
    bands: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def forward_return(points: Points, day: str, days: int) -> float | None:
    target = (date.fromisoformat(day) + timedelta(days=days)).isoformat()
    i = bisect.bisect_left(points, (target, float("-inf")))
    j = bisect.bisect_left(points, (day, float("-inf")))
    if i >= len(points) or j >= len(points) or not points[j][1]:
        return None
    return points[i][1] / points[j][1] - 1


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.0%}"


def _median(values: list[float | None]) -> str:
    vals = [v for v in values if v is not None]
    return f"{statistics.median(vals):+.0%} (n={len(vals)})" if vals else "not enough history"


def _ladder(rule: DrawdownLadder, ctx: Context) -> Replay:
    pts = ctx.series(rule.series)
    out = Replay(rule.series, downsample(pts, 900))
    if not pts:
        out.caveats.append(f"No data for {rule.series} yet.")
        return out
    steps = ladder_states(pts, [t.drawdown for t in rule.tranches], rule.reset_within)
    out.columns = ["Date", "Tranche", "Drawdown", "Index level",
                   *[f"Next {h}" for h, _ in HORIZONS]]
    fwd: dict[int, dict[str, list[float | None]]] = {}
    prev_level = -1
    for s in steps:
        if s.level > prev_level:
            for lvl in range(prev_level + 1, s.level + 1):
                rets = {h: forward_return(pts, s.date, n) for h, n in HORIZONS}
                for h, v in rets.items():
                    fwd.setdefault(lvl, {}).setdefault(h, []).append(v)
                out.markers.append({"date": s.date, "value": s.value, "label": f"T{lvl + 1}"})
                out.rows.append([s.date, f"T{lvl + 1} (-{rule.tranches[lvl].drawdown:.0%})",
                                 f"{s.drawdown:.1%}", f"{s.value:,.0f}",
                                 *[_pct(rets[h]) for h, _ in HORIZONS]])
        prev_level = s.level
    cycles = len({s.cycle_start for s in steps if s.cycle_start})
    out.summary.append(f"{cycles} drawdown cycles since {pts[0][0]} reached at least tranche 1.")
    for lvl, t in enumerate(rule.tranches):
        n = len(fwd.get(lvl, {}).get("1y", []))
        if n:
            out.summary.append(
                f"Tranche {lvl + 1} (-{t.drawdown:.0%}) hit {n} times; median return after: "
                + ", ".join(f"{h} {_median(fwd[lvl][h])}" for h, _ in HORIZONS))
        else:
            out.summary.append(f"Tranche {lvl + 1} (-{t.drawdown:.0%}) never hit in this history.")
    out.caveats.append("Price index without dividends; past drawdowns do not bound future ones.")
    return out


def _freeze(rule: ValuationFreeze, ctx: Context) -> Replay:
    pts = ctx.series(rule.series)
    price = ctx.series(rule.price_series)
    out = Replay(rule.series, downsample(pts, 900))
    if not pts:
        out.caveats.append(f"No data for {rule.series} yet.")
        return out
    states = freeze_states(pts, rule.above, rule.resume_below)
    episodes, start = [], None
    for d, frozen in states:
        if frozen and start is None:
            start = d
        elif not frozen and start is not None:
            episodes.append((start, d))
            start = None
    if start is not None:
        episodes.append((start, None))
    out.bands = [(s, e or pts[-1][0]) for s, e in episodes]
    out.columns = ["Freeze on", "Freeze off", "Months", f"{rule.price_series} during",
                   "Next 1y after it ended"]
    during = []
    for s, e in episodes:
        end = e or pts[-1][0]
        months = (date.fromisoformat(end) - date.fromisoformat(s)).days / 30.4
        days = (date.fromisoformat(end) - date.fromisoformat(s)).days
        ret = forward_return(price, s, days) if price else None
        during.append(ret)
        after = forward_return(price, e, 365) if (price and e) else None
        out.rows.append([s, e or "still on", f"{months:.0f}", _pct(ret), _pct(after)])
    frozen_share = sum(1 for _, f in states if f) / len(states)
    out.summary.append(f"{len(episodes)} freezes since {pts[0][0]}; frozen {frozen_share:.0%} "
                       "of the time.")
    if during:
        out.summary.append(f"Median {rule.price_series} move while frozen: {_median(during)}. "
                           "A positive number is growth you would not have bought into.")
    out.caveats.append("Percentiles are computed only from earlier data, so there is no "
                       "look-ahead. Monthly data; prices exclude dividends.")
    return out


def _trim(rule: TrimAtHigh, ctx: Context) -> Replay:
    key = ctx.price_series_for(rule.symbol, rule.series)
    if not key:
        return Replay(rule.symbol, [], caveats=[f"No price series for {rule.symbol}."])
    pts = ctx.series(key)
    trend = pct_above_ma(pts)
    out = Replay(key, downsample(trend, 900))
    threshold = rule.above_200dma * 100
    out.columns = ["Crossed above", "% above 200d", "Next 6m", "Next 1y"]
    above, rets_1y, rets_6m = False, [], []
    for d, v in trend:
        if v >= threshold and not above:
            r1, r6 = forward_return(pts, d, 365), forward_return(pts, d, 182)
            rets_1y.append(r1)
            rets_6m.append(r6)
            out.markers.append({"date": d, "value": v, "label": "trim?"})
            out.rows.append([d, f"{v:.1f}%", _pct(r6), _pct(r1)])
        above = v >= threshold
    out.summary.append(f"{key} crossed {threshold:.0f}% above its 200-day average "
                       f"{len(out.rows)} times.")
    if out.rows:
        out.summary.append(f"Median return after: 6m {_median(rets_6m)}, 1y {_median(rets_1y)}. "
                           "Positive means trimming gave up further gains.")
    out.caveats.append("Only the price condition is replayed; the weight condition depends on "
                       "your holdings at the time.")
    return out


def _allocation(rule: AllocationBand, ctx: Context) -> Replay:
    rows = ctx.conn.execute(
        "SELECT substr(ts, 1, 10) AS d, MAX(id) AS id FROM snapshots GROUP BY d ORDER BY d"
    ).fetchall()
    lt = load_lookthrough(get_settings().local_config)
    pts: Points = []
    for r in rows:
        snap = dict(ctx.conn.execute("SELECT * FROM snapshots WHERE id = ?", (r["id"],)).fetchone())
        pos = [dict(p) for p in ctx.conn.execute(
            "SELECT * FROM snapshot_positions WHERE snapshot_id = ?", (r["id"],))]
        view = build_view(snap, pos, ctx.cfg, lt)
        if rule.measure == "lookthrough_tech":
            v = view.tech_weight
        elif rule.measure == "cash_weight":
            v = view.cash_weight
        else:
            v = sum(view.weights.get(s, 0.0) for s in rule.symbols)
        pts.append((r["d"], v * 100))
    out = Replay("weight %", pts)
    out.summary.append(f"{len(pts)} days of your own snapshots.")
    out.caveats.append("Allocation history starts when compass began recording the account.")
    return out


def replay(conn: sqlite3.Connection, rule: Rule, cfg: PortfolioConfig) -> Replay:
    ctx = Context(conn, None, cfg)
    fn = {"drawdown_ladder": _ladder, "valuation_freeze": _freeze, "trim_at_high": _trim,
          "allocation_band": _allocation}[rule.type]
    return fn(rule, ctx)


__all__ = ["Replay", "forward_return", "get_series", "replay"]
