"""Build the context each coach question gets. The account id is never included."""
from compass.coach.llm import Coach
from compass.macro.indicators import Axis, Reading, fmt_value
from compass.portfolio.view import PortfolioView
from compass.rules.engine import Evaluation


def portfolio_brief(view: PortfolioView | None) -> str:
    if view is None:
        return "Portfolio: no snapshot from IBKR yet."
    lines = [(f"Portfolio (base {view.base_currency}, NAV about {view.nav:,.0f}, "
             f"as of {view.ts[:16]} UTC):")]
    for h in view.holdings:
        target = f", target {h.target:.0%}" if h.target is not None else ""
        lines.append(f"- {h.symbol} ({h.name}, {h.kind}): {h.weight:.1%} of NAV{target}")
    target = f", target {view.cash_target:.0%}" if view.cash_target is not None else ""
    lines.append(f"- Cash: {view.cash_weight:.1%}{target}")
    lines.append(f"Look-through tech exposure ({' + '.join(view.tech_sectors)}): "
                 f"{view.tech_weight:.1%}")
    top = ", ".join(f"{e.name} {e.total:.1%}" for e in view.names[:5])
    if top:
        lines.append(f"Largest look-through company exposures: {top}")
    return "\n".join(lines)


def reading_line(r: Reading) -> str:
    if not r.available:
        return f"- {r.name}: no data"
    pct = f", {r.percentile:.0f}th percentile since {r.history_start[:4]}" if (
        r.percentile is not None and r.history_start) else ""
    chg = f", change over {r.change_label}: {r.change:+.2f}" if r.change is not None else ""
    stale = " [STALE]" if r.stale else ""
    return f"- {r.name}: {fmt_value(r)} on {r.date}{pct}{chg}; {r.label}{stale}"


def market_brief(readings: list[Reading], axes: list[Axis]) -> str:
    lines = ["Market readings:"] + [reading_line(r) for r in readings]
    lines.append("Regime summary: " + "; ".join(f"{a.name} {a.label}" for a in axes))
    return "\n".join(lines)


def explain_indicator(coach: Coach, r: Reading, view: PortfolioView | None) -> str:
    prompt = (f"Explain this indicator to me as it stands today.\n\n{reading_line(r)}\n"
              f"Rule-of-thumb reading: {r.text}\n\n{portfolio_brief(view)}\n\n"
              "Cover: what it measures, how to read the current level against its history, what "
              "it has signalled in past cycles (with years), its blind spots, and what it means "
              "for a portfolio like mine.")
    return coach.complete("explain_indicator", prompt, cache_key=f"ind:{r.id}:{r.date}")


def explain_rule(coach: Coach, ev: Evaluation, rule_yaml: str, replay_summary: list[str],
                 view: PortfolioView | None) -> str:
    prompt = (f"Explain what this rule of mine does and how it has behaved.\n\nRule:\n{rule_yaml}\n"
              f"Today: {ev.headline}. {ev.explanation}\n"
              f"History replay: {' '.join(replay_summary) or 'not available'}\n\n"
              f"{portfolio_brief(view)}\n\nCover: the idea behind this kind of rule, what the "
              "replay shows, the main way it can go wrong, and how its thresholds could be "
              "judged (without telling me which numbers to pick).")
    key = f"rule:{ev.rule_id}:{ev.state_key}:{ev.inputs.get('date', '')}"
    return coach.complete("explain_rule", prompt, cache_key=key)


def ask(coach: Coach, question: str, readings: list[Reading], axes: list[Axis],
        view: PortfolioView | None, alerts: list[dict]) -> str:
    alert_text = "\n".join(f"- {a['name']}: {a['explanation']}" for a in alerts) or "- none"
    prompt = (f"My question: {question.strip()}\n\nContext:\n{market_brief(readings, axes)}\n\n"
              f"{portfolio_brief(view)}\n\nRules currently firing:\n{alert_text}")
    return coach.complete("ask", prompt, deep=True, max_tokens=1200)


def portfolio_review(coach: Coach, readings: list[Reading], axes: list[Axis],
                     view: PortfolioView | None, alerts: list[dict], today: str) -> str:
    alert_text = "\n".join(f"- {a['name']}: {a['explanation']}" for a in alerts) or "- none"
    prompt = ("Give me a review of where my portfolio stands in the current market cycle.\n\n"
              f"{market_brief(readings, axes)}\n\n{portfolio_brief(view)}\n\n"
              f"Rules currently firing:\n{alert_text}\n\nStructure: 1) what the macro, valuation "
              "and trend readings say together and where they disagree; 2) how my allocation is "
              "exposed to that, including overlap between funds and single stocks; 3) what my "
              "rules are saying; 4) two or three things worth learning more about this week. "
              "Up to 450 words.")
    return coach.complete("portfolio_review", prompt, deep=True, max_tokens=1600,
                          cache_key=f"review:{today}")
