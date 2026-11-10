"""Example account data for local development and screenshots. Not anyone's real portfolio.

Holdings follow the example targets in compass/defaults/portfolio.example.yaml. Prices follow
the real Nasdaq-100 and S&P 500 series when they are in the database, so charts look plausible.
"""
import math
import random
import sqlite3
from datetime import UTC, date, datetime, timedelta

from compass.db import load_series, tx
from compass.ibkr.gateway import AccountState, Position
from compass.portfolio.history import store_account

FX = {"HKD": 1.0, "USD": 7.78, "GBP": 10.05}
# symbol: (currency, exchange, driver series, start price, start quantity, beta, con_id)
HOLDINGS = {
    "EQQQ": ("GBP", "LSE", "FRED:NASDAQ100", 380.0, 21, 1.0, 1001),
    "VUAA": ("USD", "LSE", "FRED:SP500", 95.0, 54, 1.0, 1002),
    "SWRD": ("USD", "LSE", "FRED:SP500", 36.0, 108, 0.85, 1003),
    "MSFT": ("USD", "NASDAQ", "FRED:NASDAQ100", 410.0, 3, 1.1, 1004),
    "NVDA": ("USD", "NASDAQ", "FRED:NASDAQ100", 120.0, 9, 1.8, 1005),
}
START_CASH_HKD = 6000.0


class DemoRefused(RuntimeError):
    pass


def seed(conn: sqlite3.Connection, days: int = 400, seed_value: int = 7) -> int:
    if conn.execute("SELECT COUNT(*) FROM snapshots WHERE source = 'ibkr'").fetchone()[0]:
        raise DemoRefused("real IBKR snapshots exist; demo data would mix with them")
    with tx(conn):
        conn.execute("DELETE FROM snapshots WHERE source = 'demo'")
        conn.execute("DELETE FROM flex_cash WHERE transaction_id LIKE 'demo-%'")
    rng = random.Random(seed_value)
    today = date.today()
    start = today - timedelta(days=days)
    drivers = {k: {d: v for d, v in load_series(conn, k, start.isoformat())}
               for k in ("FRED:NASDAQ100", "FRED:SP500")}
    walk = {k: 1.0 for k in drivers}
    first = {k: None for k in drivers}
    count = 0
    day = start
    while day <= today:
        if day.weekday() < 5:
            levels = {}
            for k, series in drivers.items():
                if series:
                    v = series.get(day.isoformat())
                    if v is None:
                        continue
                    first[k] = first[k] or v
                    levels[k] = v / first[k]
                else:
                    walk[k] *= math.exp(rng.gauss(0.0004, 0.012))
                    levels[k] = walk[k]
            if len(levels) == len(drivers):
                _snapshot(conn, day, levels)
                count += 1
        day += timedelta(days=1)
    with tx(conn):
        for i, months_ago in enumerate((12, 9, 6, 3)):
            when = (today - timedelta(days=30 * months_ago)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO flex_cash(transaction_id, date, type, currency, amount, "
                "fx_to_base, description) VALUES (?, ?, 'Deposits/Withdrawals', 'HKD', ?, 1, ?)",
                (f"demo-{i}", when, 5000.0, "Demo deposit"))
    return count


def _snapshot(conn: sqlite3.Connection, day: date, levels: dict[str, float]) -> None:
    positions = []
    for symbol, (ccy, exch, driver, p0, qty, beta, con_id) in HOLDINGS.items():
        rel = levels[driver] ** beta
        price = p0 * rel
        mv = price * qty
        positions.append(Position(
            symbol=symbol, con_id=con_id, exchange=exch, currency=ccy, sec_type="STK",
            quantity=qty, avg_cost=p0, market_price=price, market_value=mv,
            fx_to_base=FX[ccy], market_value_base=mv * FX[ccy],
            unrealized_pnl=(price - p0) * qty, realized_pnl=0.0))
    invested = sum(p.market_value_base for p in positions)
    ts = datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC).isoformat()
    state = AccountState(
        ts=ts, account="DEMO", base_currency="HKD", nav_base=invested + START_CASH_HKD,
        cash_base=START_CASH_HKD,
        unrealized_pnl_base=sum((p.unrealized_pnl or 0) * p.fx_to_base for p in positions),
        realized_pnl_base=0.0, fx=dict(FX), positions=positions)
    store_account(conn, state, source="demo", daily_close=True)
