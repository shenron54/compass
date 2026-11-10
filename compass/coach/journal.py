"""Decision journal: write down why, then let the coach compare it with what happened."""
import json
import sqlite3
from datetime import UTC, date, datetime

from compass.coach.llm import Coach
from compass.macro.series import get_series
from compass.portfolio.view import PortfolioView

ACTIONS = ("buy", "sell", "trim", "add", "hold", "watch", "rule change")
HORIZONS = (30, 90, 365)
INDEXES = ("FRED:NASDAQ100", "FRED:SP500")


def _price_map(conn: sqlite3.Connection, view: PortfolioView | None,
               symbols: list[str]) -> dict[str, dict]:
    prices: dict[str, dict] = {}
    if view:
        for h in view.holdings:
            if h.symbol in symbols and h.market_price is not None:
                prices[h.symbol] = {"price": h.market_price, "currency": h.currency}
    for key in INDEXES:
        pts = get_series(conn, key)
        if pts:
            prices[key] = {"price": pts[-1][1], "date": pts[-1][0]}
    return prices


def create_entry(conn: sqlite3.Connection, view: PortfolioView | None, *, title: str,
                 action: str, symbols: str, thesis: str, trigger: str = "",
                 rule_id: str | None = None, expected: str = "") -> int:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if not title.strip() or not thesis.strip():
        raise ValueError("title and thesis are required")
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    cur = conn.execute(
        "INSERT INTO journal(created_at, title, action, symbols, thesis, trigger, rule_id, "
        "expected, prices_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).replace(microsecond=0).isoformat(), title.strip(), action,
         ",".join(syms), thesis.strip(), trigger.strip(), rule_id or None, expected.strip(),
         json.dumps(_price_map(conn, view, syms))))
    return cur.lastrowid


def entries(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM journal ORDER BY created_at DESC"):
        e = dict(row)
        e["reviews"] = [dict(r) for r in conn.execute(
            "SELECT * FROM journal_reviews WHERE entry_id = ? ORDER BY horizon_days", (e["id"],))]
        e["prices"] = json.loads(e["prices_json"])
        out.append(e)
    return out


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    return next((e for e in entries(conn) if e["id"] == entry_id), None)


def due_reviews(conn: sqlite3.Connection, today: date | None = None) -> list[tuple[dict, int]]:
    today = today or date.today()
    due = []
    for e in entries(conn):
        age = (today - datetime.fromisoformat(e["created_at"]).date()).days
        done = {r["horizon_days"] for r in e["reviews"]}
        for h in HORIZONS:
            if age >= h and h not in done:
                due.append((e, h))
                break
    return due


def price_changes(conn: sqlite3.Connection, entry: dict,
                  view: PortfolioView | None) -> list[str]:
    now = _price_map(conn, view, entry["symbols"].split(",") if entry["symbols"] else [])
    lines = []
    for key, then in entry["prices"].items():
        cur = now.get(key)
        if cur and then.get("price"):
            change = cur["price"] / then["price"] - 1
            lines.append(f"{key}: {then['price']:,.2f} then, {cur['price']:,.2f} now ({change:+.1%})")
        else:
            lines.append(f"{key}: {then.get('price')} then, no current price")
    return lines


def review_entry(coach: Coach, conn: sqlite3.Connection, entry: dict, horizon: int,
                 view: PortfolioView | None) -> str:
    changes = "\n".join(price_changes(conn, entry, view)) or "no prices recorded"
    prompt = (f"Review a decision from my journal, {horizon} days on.\n\n"
              f"Written {entry['created_at'][:10]}: {entry['title']} ({entry['action']} "
              f"{entry['symbols'] or 'no symbol'})\nThesis: {entry['thesis']}\n"
              f"Trigger: {entry['trigger'] or '-'}\nRule: {entry['rule_id'] or '-'}\n"
              f"What I expected: {entry['expected'] or '-'}\n\nPrice changes since:\n{changes}\n\n"
              "Judge the reasoning separately from the outcome: was the thesis tested yet, did "
              "anything I expected happen, was the result luck or process, and what should I "
              "watch next. Under 250 words.")
    text = coach.complete("journal_review", prompt, max_tokens=900)
    conn.execute(
        "INSERT OR REPLACE INTO journal_reviews(entry_id, horizon_days, created_at, text) "
        "VALUES (?, ?, datetime('now'), ?)", (entry["id"], horizon, text))
    return text
