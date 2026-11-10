import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from compass.db import tx
from compass.portfolio.config import PortfolioConfig
from compass.portfolio.view import PortfolioView
from compass.rules.engine import Context, Evaluation, evaluate
from compass.rules.models import Rule, dump_rule, parse_rule_yaml, parse_rules_file


def seed_rules(conn: sqlite3.Connection, local_dir: Path) -> int:
    if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]:
        return 0
    local = local_dir / "rules.yaml"
    text = (local.read_text() if local.exists() else
            resources.files("compass.defaults").joinpath("rules.example.yaml").read_text())
    rules = parse_rules_file(text)
    with tx(conn):
        for r in rules:
            conn.execute("INSERT INTO rules(id, yaml, enabled) VALUES (?, ?, ?)",
                         (r.id, dump_rule(r), int(r.enabled)))
    return len(rules)


def load_rules(conn: sqlite3.Connection, only_enabled: bool = False) -> list[tuple[Rule, str]]:
    sql = "SELECT id, yaml, enabled FROM rules"
    if only_enabled:
        sql += " WHERE enabled = 1"
    out = []
    for row in conn.execute(sql + " ORDER BY id"):
        rule = parse_rule_yaml(row["yaml"])
        rule.enabled = bool(row["enabled"])
        out.append((rule, row["yaml"]))
    return out


def save_rule(conn: sqlite3.Connection, text: str, original_id: str | None = None) -> Rule:
    rule = parse_rule_yaml(text)
    with tx(conn):
        if original_id and original_id != rule.id:
            conn.execute("DELETE FROM rules WHERE id = ?", (original_id,))
        conn.execute(
            "INSERT INTO rules(id, yaml, enabled, updated_at) VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(id) DO UPDATE SET yaml = excluded.yaml, enabled = excluded.enabled, "
            "updated_at = excluded.updated_at", (rule.id, text, int(rule.enabled)))
    return rule


def delete_rule(conn: sqlite3.Connection, rule_id: str) -> None:
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def set_enabled(conn: sqlite3.Connection, rule_id: str, enabled: bool) -> None:
    conn.execute("UPDATE rules SET enabled = ? WHERE id = ?", (int(enabled), rule_id))


def evaluate_all(conn: sqlite3.Connection, view: PortfolioView | None,
                 cfg: PortfolioConfig, record: bool = True) -> list[Evaluation]:
    ctx = Context(conn, view, cfg)
    results = [evaluate(rule, ctx) for rule, _ in load_rules(conn, only_enabled=True)]
    if record:
        for ev in results:
            _record(conn, ev)
    return results


def _record(conn: sqlite3.Connection, ev: Evaluation) -> None:
    """Store an event only when the rule changes state, so the log reads as a history of turns."""
    last = conn.execute(
        "SELECT fired, state_key FROM rule_events WHERE rule_id = ? ORDER BY id DESC LIMIT 1",
        (ev.rule_id,)).fetchone()
    if last and last["fired"] == int(ev.fired) and last["state_key"] == ev.state_key:
        return
    conn.execute(
        "INSERT INTO rule_events(ts, rule_id, rule_type, fired, state_key, explanation, "
        "inputs_json, suggestions_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).replace(microsecond=0).isoformat(), ev.rule_id, ev.rule_type,
         int(ev.fired), ev.state_key, f"{ev.headline}. {ev.explanation}",
         json.dumps(ev.inputs), json.dumps(ev.suggestions)))


def active_alerts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT e.*, r.yaml FROM rule_events e JOIN rules r ON r.id = e.rule_id "
        "WHERE r.enabled = 1 AND e.id IN (SELECT MAX(id) FROM rule_events GROUP BY rule_id) "
        "AND e.fired = 1 AND e.dismissed = 0 ORDER BY e.ts DESC").fetchall()
    out = []
    for r in rows:
        rule = parse_rule_yaml(r["yaml"])
        out.append({"id": r["id"], "rule_id": r["rule_id"], "name": rule.name, "ts": r["ts"],
                    "explanation": r["explanation"],
                    "suggestions": json.loads(r["suggestions_json"])})
    return out


def dismiss(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("UPDATE rule_events SET dismissed = 1 WHERE id = ?", (event_id,))


def events_for(conn: sqlite3.Connection, rule_id: str, limit: int = 50) -> list[dict]:
    return [dict(r) | {"suggestions": json.loads(r["suggestions_json"])} for r in conn.execute(
        "SELECT * FROM rule_events WHERE rule_id = ? ORDER BY id DESC LIMIT ?", (rule_id, limit))]


def as_dict(ev: Evaluation) -> dict:
    return asdict(ev)
