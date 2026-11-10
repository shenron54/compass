"""Refresh jobs shared by the worker schedule and the CLI."""
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import httpx

from compass.coach.journal import due_reviews, review_entry
from compass.coach.llm import Coach, CoachError
from compass.config import Settings
from compass.data import fred, multpl
from compass.data.http import client
from compass.db import load_series, record_job, upsert_series
from compass.ibkr import flex, gateway
from compass.macro.series import FRED_SERIES, MULTPL_SERIES, days_before
from compass.portfolio.config import load_lookthrough, load_portfolio_config
from compass.portfolio.history import prune_intraday, store_account, store_flex
from compass.portfolio.view import PortfolioView, latest_snapshot, load_view
from compass.rules.store import evaluate_all, seed_rules

log = logging.getLogger(__name__)

US = (ZoneInfo("America/New_York"), time(9, 30), time(16, 0))
LSE = (ZoneInfo("Europe/London"), time(8, 0), time(16, 30))


def market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    for tz, start, end in (US, LSE):
        local = now.astimezone(tz)
        if local.weekday() < 5 and start <= local.time() <= end:
            return True
    return False


def current_view(conn: sqlite3.Connection, settings: Settings) -> PortfolioView | None:
    cfg = load_portfolio_config(settings.local_config)
    return load_view(conn, cfg, load_lookthrough(settings.local_config))


def run(conn: sqlite3.Connection, name: str, fn: Callable[[], str | None]) -> bool:
    try:
        detail = fn()
    except Exception as e:  # every job failure is recorded and shown on the status page
        log.exception("job %s failed", name)
        record_job(conn, name, error=f"{type(e).__name__}: {e}")
        return False
    record_job(conn, name, detail=detail)
    return True


def refresh_macro(conn: sqlite3.Connection, settings: Settings,
                  http: httpx.Client | None = None) -> str:
    http = http or client()
    done, failed = 0, []
    for sid in FRED_SERIES:
        key = f"FRED:{sid}"
        existing = load_series(conn, key)
        # Refetch the last 90 days each time: FRED revises recent observations.
        start = days_before(existing[-1][0], 90) if existing else None
        try:
            points, url = fred.fetch(sid, http, settings.fred_api_key, start)
            upsert_series(conn, key, points, "FRED", url)
            done += 1
        except (fred.FredError, httpx.HTTPError, ValueError) as e:
            failed.append(sid)
            _series_error(conn, key, "FRED", e)
    for name in MULTPL_SERIES:
        key = f"MULTPL:{name}"
        try:
            points, url = multpl.fetch(key, http)
            upsert_series(conn, key, points, "multpl", url)
            done += 1
        except (multpl.MultplError, httpx.HTTPError, ValueError) as e:
            failed.append(key)
            _series_error(conn, key, "multpl", e)
    if failed and not done:
        raise RuntimeError(f"every series failed: {', '.join(failed)}")
    return f"{done} series updated" + (f"; failed: {', '.join(failed)}" if failed else "")


def _series_error(conn: sqlite3.Connection, key: str, source: str, e: Exception) -> None:
    log.warning("series %s failed: %s", key, e)
    conn.execute(
        "INSERT INTO series_meta(series_id, source, updated_at, last_error) "
        "VALUES (?, ?, datetime('now'), ?) ON CONFLICT(series_id) DO UPDATE SET "
        "last_error = excluded.last_error", (key, source, f"{type(e).__name__}: {e}"[:500]))


def poll_ibkr(conn: sqlite3.Connection, settings: Settings, daily_close: bool = False) -> str:
    state = gateway.fetch_account(settings.ib_host, settings.ib_port, settings.ib_client_id,
                                  settings.ib_account)
    store_account(conn, state, daily_close=daily_close)
    pruned = prune_intraday(conn)
    return (f"{len(state.positions)} positions, NAV {state.nav_base:,.0f} {state.base_currency}"
            + (f"; pruned {pruned}" if pruned else ""))


def poll_if_due(conn: sqlite3.Connection, settings: Settings) -> str | None:
    """Every 5 minutes while US or London trade; hourly otherwise (cash and FX still move)."""
    latest = latest_snapshot(conn)
    if not market_open() and latest and latest[0]["source"] == "ibkr":
        age = datetime.now(UTC) - datetime.fromisoformat(latest[0]["ts"])
        if age.total_seconds() < 3600:
            return None
    return poll_ibkr(conn, settings)


def pull_flex(conn: sqlite3.Connection, settings: Settings,
              http: httpx.Client | None = None) -> str:
    report = flex.fetch_report(settings.flex_token, settings.flex_query_id, http or client())
    counts = store_flex(conn, report)
    return ", ".join(f"{v} {k}" for k, v in counts.items())


def refresh_ib_history(conn: sqlite3.Connection, settings: Settings) -> str:
    latest = latest_snapshot(conn)
    if not latest:
        return "no positions yet"
    con_ids = {p["symbol"]: p["con_id"] for p in latest[1] if p["con_id"]}
    results = gateway.fetch_daily_bars(settings.ib_host, settings.ib_port,
                                       settings.ib_client_id + 1, con_ids)
    ok, errors = [], []
    for symbol, res in results.items():
        if isinstance(res, str):
            errors.append(f"{symbol}: {res}")
            _series_error(conn, f"IB:{symbol}", "IBKR", RuntimeError(res))
        else:
            upsert_series(conn, f"IB:{symbol}", res, "IBKR")
            ok.append(symbol)
    return f"history for {', '.join(ok) or 'none'}" + (f"; {'; '.join(errors)}" if errors else "")


def evaluate_rules(conn: sqlite3.Connection, settings: Settings) -> str:
    seed_rules(conn, settings.local_config)
    cfg = load_portfolio_config(settings.local_config)
    view = load_view(conn, cfg, load_lookthrough(settings.local_config))
    results = evaluate_all(conn, view, cfg)
    fired = [r.rule_id for r in results if r.fired]
    return f"{len(results)} rules, firing: {', '.join(fired) or 'none'}"


def review_journal(conn: sqlite3.Connection, settings: Settings, today: date | None = None) -> str:
    coach = Coach(conn, settings)
    if not coach.available:
        return "coach not configured; reviews wait"
    view = current_view(conn, settings)
    done = 0
    for entry, horizon in due_reviews(conn, today):
        try:
            review_entry(coach, conn, entry, horizon, view)
            done += 1
        except CoachError as e:
            return f"{done} reviews written; stopped: {e}"
    return f"{done} reviews written"
