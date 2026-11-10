import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DEPOSIT_TYPES = ("Deposits/Withdrawals", "Deposits & Withdrawals")


def local_date(ts: str, tz: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).date().isoformat()


def nav_series(conn: sqlite3.Connection, tz: str) -> list[tuple[str, float, str]]:
    """Daily NAV in base currency as (date, nav, source).

    The Flex statement is IBKR's official end-of-day figure, so it wins on any date it covers;
    the app's own snapshots fill the days since the last statement.
    """
    by_date: dict[str, tuple[float, str]] = {}
    for r in conn.execute("SELECT ts, nav_base FROM snapshots ORDER BY ts"):
        by_date[local_date(r["ts"], tz)] = (r["nav_base"], "snapshot")
    for r in conn.execute("SELECT date, total_base FROM flex_nav ORDER BY date"):
        by_date[r["date"]] = (r["total_base"], "flex")
    return [(d, v, s) for d, (v, s) in sorted(by_date.items())]


def drawdowns(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    out, peak = [], float("-inf")
    for d, v in points:
        peak = max(peak, v)
        out.append((d, v / peak - 1 if peak > 0 else 0.0))
    return out


def net_contributions(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """Cumulative deposits minus withdrawals in base currency, one point per cash movement."""
    placeholders = ",".join("?" * len(DEPOSIT_TYPES))
    rows = conn.execute(
        f"SELECT date, amount, COALESCE(fx_to_base, 1) AS fx FROM flex_cash "
        f"WHERE type IN ({placeholders}) ORDER BY date", DEPOSIT_TYPES,
    ).fetchall()
    total, out = 0.0, []
    for r in rows:
        total += r["amount"] * r["fx"]
        if out and out[-1][0] == r["date"]:
            out[-1] = (r["date"], total)
        else:
            out.append((r["date"], total))
    return out


def store_flex(conn: sqlite3.Connection, report) -> dict[str, int]:
    from compass.db import tx

    with tx(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO flex_trades(trade_id, trade_date, symbol, asset_category, "
            "currency, quantity, price, proceeds, commission, fx_to_base, buy_sell) VALUES "
            "(:trade_id, :trade_date, :symbol, :asset_category, :currency, :quantity, :price, "
            ":proceeds, :commission, :fx_to_base, :buy_sell)", report.trades)
        conn.executemany(
            "INSERT OR REPLACE INTO flex_cash(transaction_id, date, type, currency, amount, "
            "fx_to_base, symbol, description) VALUES (:transaction_id, :date, :type, :currency, "
            ":amount, :fx_to_base, :symbol, :description)", report.cash)
        conn.executemany(
            "INSERT OR REPLACE INTO flex_nav(date, total_base, cash_base, stock_base) VALUES "
            "(:date, :total_base, :cash_base, :stock_base)", report.nav)
    return {"trades": len(report.trades), "cash": len(report.cash), "nav": len(report.nav)}


def store_account(conn: sqlite3.Connection, state, source: str = "ibkr",
                  daily_close: bool = False) -> int:
    from compass.db import tx

    with tx(conn):
        cur = conn.execute(
            "INSERT INTO snapshots(ts, source, account, base_currency, nav_base, cash_base, "
            "unrealized_pnl_base, realized_pnl_base, is_daily_close) VALUES (?,?,?,?,?,?,?,?,?)",
            (state.ts, source, state.account, state.base_currency, state.nav_base,
             state.cash_base, state.unrealized_pnl_base, state.realized_pnl_base,
             int(daily_close)),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO snapshot_positions(snapshot_id, symbol, con_id, exchange, currency, "
            "sec_type, quantity, avg_cost, market_price, market_value, fx_to_base, "
            "market_value_base, unrealized_pnl, realized_pnl) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, p.symbol, p.con_id, p.exchange, p.currency, p.sec_type, p.quantity,
              p.avg_cost, p.market_price, p.market_value, p.fx_to_base, p.market_value_base,
              p.unrealized_pnl, p.realized_pnl) for p in state.positions],
        )
    return sid


def prune_intraday(conn: sqlite3.Connection, keep_days: int = 14) -> int:
    """Five-minute snapshots matter for today; after two weeks keep one per day."""
    cur = conn.execute(
        "DELETE FROM snapshots WHERE is_daily_close = 0 AND ts < datetime('now', ?) "
        "AND id NOT IN (SELECT MAX(id) FROM snapshots GROUP BY substr(ts, 1, 10))",
        (f"-{keep_days} days",),
    )
    return cur.rowcount
