import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from compass.config import get_settings

# Append only: each entry runs once, in order, tracked by PRAGMA user_version.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        totp_secret TEXT NOT NULL,
        session_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL DEFAULT (datetime('now')),
        ip TEXT NOT NULL,
        ok INTEGER NOT NULL
    );
    CREATE INDEX login_attempts_ts ON login_attempts(ts);

    CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        source TEXT NOT NULL,
        account TEXT NOT NULL,
        base_currency TEXT NOT NULL,
        nav_base REAL NOT NULL,
        cash_base REAL NOT NULL,
        unrealized_pnl_base REAL,
        realized_pnl_base REAL,
        is_daily_close INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX snapshots_ts ON snapshots(ts);
    CREATE TABLE snapshot_positions (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL,
        con_id INTEGER,
        exchange TEXT,
        currency TEXT NOT NULL,
        sec_type TEXT,
        quantity REAL NOT NULL,
        avg_cost REAL,
        market_price REAL,
        market_value REAL NOT NULL,
        fx_to_base REAL NOT NULL,
        market_value_base REAL NOT NULL,
        unrealized_pnl REAL,
        realized_pnl REAL
    );
    CREATE INDEX snapshot_positions_snap ON snapshot_positions(snapshot_id);

    CREATE TABLE series (
        series_id TEXT NOT NULL,
        date TEXT NOT NULL,
        value REAL NOT NULL,
        PRIMARY KEY (series_id, date)
    ) WITHOUT ROWID;
    CREATE TABLE series_meta (
        series_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_url TEXT,
        updated_at TEXT NOT NULL,
        last_error TEXT
    );

    CREATE TABLE flex_trades (
        trade_id TEXT PRIMARY KEY,
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        asset_category TEXT,
        currency TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL,
        proceeds REAL,
        commission REAL,
        fx_to_base REAL,
        buy_sell TEXT
    );
    CREATE TABLE flex_cash (
        transaction_id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        currency TEXT NOT NULL,
        amount REAL NOT NULL,
        fx_to_base REAL,
        symbol TEXT,
        description TEXT
    );
    CREATE TABLE flex_nav (
        date TEXT PRIMARY KEY,
        total_base REAL NOT NULL,
        cash_base REAL,
        stock_base REAL
    );

    CREATE TABLE rules (
        id TEXT PRIMARY KEY,
        yaml TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE rule_events (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        rule_id TEXT NOT NULL,
        rule_type TEXT NOT NULL,
        fired INTEGER NOT NULL,
        state_key TEXT,
        explanation TEXT NOT NULL,
        inputs_json TEXT NOT NULL,
        suggestions_json TEXT NOT NULL,
        dismissed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX rule_events_rule_ts ON rule_events(rule_id, ts);

    CREATE TABLE journal (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        action TEXT NOT NULL,
        symbols TEXT NOT NULL DEFAULT '',
        thesis TEXT NOT NULL,
        trigger TEXT NOT NULL DEFAULT '',
        rule_id TEXT,
        expected TEXT NOT NULL DEFAULT '',
        prices_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE journal_reviews (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES journal(id) ON DELETE CASCADE,
        horizon_days INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        text TEXT NOT NULL,
        UNIQUE (entry_id, horizon_days)
    );

    CREATE TABLE llm_usage (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL DEFAULT (datetime('now')),
        model TEXT NOT NULL,
        purpose TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL
    );
    CREATE TABLE coach_notes (
        key TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        model TEXT NOT NULL,
        text TEXT NOT NULL
    );
    CREATE TABLE job_runs (
        job TEXT PRIMARY KEY,
        last_ok TEXT,
        last_error TEXT,
        last_error_at TEXT,
        detail TEXT
    );
    """,
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def upsert_series(conn: sqlite3.Connection, series_id: str, points: list[tuple[str, float]],
                  source: str, source_url: str | None = None) -> int:
    with tx(conn):
        conn.executemany(
            "INSERT INTO series(series_id, date, value) VALUES (?, ?, ?) "
            "ON CONFLICT(series_id, date) DO UPDATE SET value = excluded.value",
            [(series_id, d, v) for d, v in points],
        )
        conn.execute(
            "INSERT INTO series_meta(series_id, source, source_url, updated_at, last_error) "
            "VALUES (?, ?, ?, datetime('now'), NULL) ON CONFLICT(series_id) DO UPDATE SET "
            "source = excluded.source, source_url = excluded.source_url, "
            "updated_at = excluded.updated_at, last_error = NULL",
            (series_id, source, source_url),
        )
    return len(points)


def load_series(conn: sqlite3.Connection, series_id: str,
                start: str | None = None) -> list[tuple[str, float]]:
    if start:
        rows = conn.execute(
            "SELECT date, value FROM series WHERE series_id = ? AND date >= ? ORDER BY date",
            (series_id, start),
        )
    else:
        rows = conn.execute(
            "SELECT date, value FROM series WHERE series_id = ? ORDER BY date", (series_id,)
        )
    return [(r[0], r[1]) for r in rows]


def record_job(conn: sqlite3.Connection, job: str, error: str | None = None,
               detail: str | None = None) -> None:
    if error is None:
        conn.execute(
            "INSERT INTO job_runs(job, last_ok, detail) VALUES (?, datetime('now'), ?) "
            "ON CONFLICT(job) DO UPDATE SET last_ok = excluded.last_ok, detail = excluded.detail",
            (job, detail),
        )
    else:
        conn.execute(
            "INSERT INTO job_runs(job, last_error, last_error_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(job) DO UPDATE SET last_error = excluded.last_error, "
            "last_error_at = excluded.last_error_at",
            (job, error[:1000]),
        )
