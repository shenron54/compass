"""Evaluate rules against the latest portfolio and market data.

A rule only ever produces text: whether it fired, why, and what the rule itself says to do.
Nothing here can place an order.
"""
import sqlite3
from dataclasses import dataclass, field

from compass.macro.series import Points, get_series, pct_above_ma
from compass.portfolio.config import PortfolioConfig
from compass.portfolio.view import PortfolioView
from compass.rules.models import AllocationBand, DrawdownLadder, Rule, TrimAtHigh, ValuationFreeze


@dataclass
class Evaluation:
    rule_id: str
    rule_type: str
    name: str
    fired: bool
    state_key: str
    headline: str
    explanation: str
    inputs: dict = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    error: str | None = None


class Context:
    def __init__(self, conn: sqlite3.Connection, view: PortfolioView | None,
                 cfg: PortfolioConfig):
        self.conn, self.view, self.cfg = conn, view, cfg
        self._cache: dict[str, Points] = {}

    def series(self, key: str) -> Points:
        if key not in self._cache:
            self._cache[key] = get_series(self.conn, key)
        return self._cache[key]

    def price_series_for(self, symbol: str, explicit: str | None) -> str | None:
        if explicit:
            return explicit
        if self.series(f"IB:{symbol}"):
            return f"IB:{symbol}"
        return self.cfg.price_series.get(symbol)


def _money(amount: float, ccy: str) -> str:
    return f"{amount:,.0f} {ccy}"


# --- state machines shared with the history replay -------------------------------------------

def freeze_states(points: Points, above: float, resume_below: float) -> list[tuple[str, bool]]:
    frozen, out = False, []
    for d, v in points:
        if v >= above:
            frozen = True
        elif v <= resume_below:
            frozen = False
        out.append((d, frozen))
    return out


@dataclass
class LadderStep:
    date: str
    value: float
    drawdown: float
    level: int
    cycle_start: str | None
    peak: float


def ladder_states(points: Points, thresholds: list[float], reset_within: float) -> list[LadderStep]:
    """Walk the series: a cycle starts when the drawdown reaches the first tranche and ends when
    the price is back within reset_within of its running high. level is the deepest tranche
    reached in the current cycle (-1 outside a cycle)."""
    peak, level, cycle_start, out = float("-inf"), -1, None, []
    for d, v in points:
        peak = max(peak, v)
        dd = v / peak - 1 if peak > 0 else 0.0
        if dd >= -reset_within:
            level, cycle_start = -1, None
        else:
            reached = max((i for i, t in enumerate(thresholds) if dd <= -t), default=-1)
            if reached > level:
                if level == -1:
                    cycle_start = d
                level = reached
        out.append(LadderStep(d, v, dd, level, cycle_start, peak))
    return out


# --- evaluators ------------------------------------------------------------------------------

def _allocation(rule: AllocationBand, ctx: Context) -> Evaluation:
    view = ctx.view
    if view is None:
        raise LookupError("no portfolio snapshot yet")
    if rule.measure == "lookthrough_tech":
        value, what = view.tech_weight, "Look-through tech exposure"
    elif rule.measure == "cash_weight":
        value, what = view.cash_weight, "Cash"
    else:
        value = sum(view.weights.get(s, 0.0) for s in rule.symbols)
        what = " + ".join(rule.symbols)
    ccy, nav = view.base_currency, view.nav
    over = rule.max is not None and value > rule.max
    under = rule.min is not None and value < rule.min
    bounds = " to ".join(f"{b:.0%}" for b in (rule.min, rule.max) if b is not None)
    inputs = {"value": round(value, 4), "min": rule.min, "max": rule.max, "nav": round(nav, 2)}
    if over:
        gap = (value - rule.max) * nav
        sugg = [(f"Bringing {what.lower()} down to {rule.max:.0%} means moving about "
                f"{_money(gap, ccy)} out of it, or adding new money elsewhere until the share "
                "falls.")]
        return Evaluation(rule.id, rule.type, rule.name, True, "over",
                          f"{what} {value:.1%} is above {rule.max:.0%}",
                          f"{what} is {value:.1%} of NAV; the rule allows {bounds}.", inputs, sugg)
    if under:
        gap = (rule.min - value) * nav
        sugg = [f"Reaching {rule.min:.0%} needs about {_money(gap, ccy)} more in {what.lower()}."]
        return Evaluation(rule.id, rule.type, rule.name, True, "under",
                          f"{what} {value:.1%} is below {rule.min:.0%}",
                          f"{what} is {value:.1%} of NAV; the rule allows {bounds}.", inputs, sugg)
    return Evaluation(rule.id, rule.type, rule.name, False, "ok",
                      f"{what} {value:.1%} is inside {bounds}",
                      f"{what} is {value:.1%} of NAV, inside the {bounds} band.", inputs)


def _freeze(rule: ValuationFreeze, ctx: Context) -> Evaluation:
    pts = ctx.series(rule.series)
    if not pts:
        raise LookupError(f"no data for {rule.series}")
    states = freeze_states(pts, rule.above, rule.resume_below)
    d, v = pts[-1]
    frozen = states[-1][1]
    since = d
    for sd, s in reversed(states):
        if s != frozen:
            break
        since = sd
    inputs = {"series": rule.series, "date": d, "value": round(v, 2), "above": rule.above,
              "resume_below": rule.resume_below, "since": since}
    if frozen:
        return Evaluation(
            rule.id, rule.type, rule.name, True, f"frozen:{since}",
            f"Freeze on: {rule.series} {v:.1f} (since {since})",
            f"{rule.series} is {v:.1f} on {d}. The freeze turned on at {rule.above:g} and stays on "
            f"until it falls to {rule.resume_below:g}.", inputs,
            [(f"The rule says: pause {rule.applies_to}. Keep new money as cash until "
             f"{rule.series} is back at or below {rule.resume_below:g}.")])
    return Evaluation(rule.id, rule.type, rule.name, False, "open",
                      f"No freeze: {rule.series} {v:.1f}",
                      f"{rule.series} is {v:.1f} on {d}; the freeze starts at {rule.above:g}.",
                      inputs)


def _ladder(rule: DrawdownLadder, ctx: Context) -> Evaluation:
    pts = ctx.series(rule.series)
    if not pts:
        raise LookupError(f"no data for {rule.series}")
    steps = ladder_states(pts, [t.drawdown for t in rule.tranches], rule.reset_within)
    last = steps[-1]
    inputs = {"series": rule.series, "date": last.date, "value": round(last.value, 2),
              "peak": round(last.peak, 2), "drawdown": round(last.drawdown, 4),
              "level": last.level}
    if last.level < 0:
        nxt = rule.tranches[0].drawdown
        return Evaluation(rule.id, rule.type, rule.name, False, "idle",
                          f"{rule.series} {last.drawdown:.1%} from its high",
                          f"{rule.series} is {abs(last.drawdown):.1%} below its high; the first "
                          f"tranche is at -{nxt:.0%}.", inputs)
    view = ctx.view
    ccy = view.base_currency if view else ctx.cfg.base_currency
    if rule.budget is not None:
        budget, budget_note = rule.budget, "the rule's fixed budget"
    elif view is not None:
        budget, budget_note = max(view.cash - rule.cash_floor, 0.0), "current cash above the floor"
    else:
        budget, budget_note = 0.0, "no portfolio snapshot yet"
    inputs["budget"] = round(budget, 2)
    sugg = []
    for i, t in enumerate(rule.tranches[: last.level + 1]):
        into = f" into {t.buy}" if t.buy else ""
        sugg.append(f"Tranche {i + 1} (-{t.drawdown:.0%}): about {_money(t.fraction * budget, ccy)}"
                    f"{into} ({t.fraction:.0%} of {budget_note}).")
    tranche = rule.tranches[last.level]
    return Evaluation(
        rule.id, rule.type, rule.name, True, f"{last.cycle_start}:t{last.level + 1}",
        f"Tranche {last.level + 1} reached: {rule.series} {last.drawdown:.1%}",
        f"{rule.series} is {abs(last.drawdown):.1%} below its high of {last.peak:,.0f}. This cycle "
        f"started on {last.cycle_start}; tranches up to -{tranche.drawdown:.0%} have been reached. "
        "Record in the journal what you actually buy.", inputs, sugg)


def _trim(rule: TrimAtHigh, ctx: Context) -> Evaluation:
    key = ctx.price_series_for(rule.symbol, rule.series)
    if not key:
        raise LookupError(f"no price series for {rule.symbol}; set 'series' on the rule")
    trend = pct_above_ma(ctx.series(key))
    if not trend:
        raise LookupError(f"not enough history in {key} for a 200-day average")
    d, pct = trend[-1]
    view = ctx.view
    target = rule.target if rule.target is not None else ctx.cfg.targets.get(rule.symbol)
    weight = view.weights.get(rule.symbol) if view else None
    inputs = {"series": key, "date": d, "pct_above_200dma": round(pct, 2),
              "weight": None if weight is None else round(weight, 4), "target": target}
    extended = pct >= rule.above_200dma * 100
    overweight = (weight is not None and target is not None
                  and weight >= target + rule.overweight_by)
    weight_note = ("weight unknown (no snapshot or no target)" if weight is None or target is None
                   else f"weight {weight:.1%} vs target {target:.0%}")
    if extended and overweight:
        amount = (weight - target) * view.nav
        return Evaluation(
            rule.id, rule.type, rule.name, True, "trim",
            f"{rule.symbol}: {pct:.0f}% above trend and overweight",
            f"{key} is {pct:.1f}% above its 200-day average (rule: {rule.above_200dma:.0%}) and "
            f"{weight_note}.", inputs,
            [(f"Trimming {rule.symbol} back to {target:.0%} means selling about "
             f"{_money(amount, view.base_currency)}.")])
    why = []
    if not extended:
        why.append(f"{pct:.1f}% above the 200-day average, under {rule.above_200dma:.0%}")
    if not overweight:
        why.append(weight_note)
    return Evaluation(rule.id, rule.type, rule.name, False, "hold",
                      f"{rule.symbol}: no trim ({pct:+.0f}% vs trend)", "; ".join(why) + ".",
                      inputs)


EVALUATORS = {"allocation_band": _allocation, "valuation_freeze": _freeze,
              "drawdown_ladder": _ladder, "trim_at_high": _trim}


def evaluate(rule: Rule, ctx: Context) -> Evaluation:
    try:
        return EVALUATORS[rule.type](rule, ctx)
    except LookupError as e:
        return Evaluation(rule.id, rule.type, rule.name, False, "error",
                          f"Cannot evaluate: {e}", str(e), error=str(e))
