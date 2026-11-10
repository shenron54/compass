"""Read-only access to IB Gateway through ib_async.

This module only reads: positions, account values and historical bars. It must never call any
order method; tests/test_no_orders.py fails the build if one appears anywhere in the package.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    con_id: int | None
    exchange: str
    currency: str
    sec_type: str
    quantity: float
    avg_cost: float | None
    market_price: float | None
    market_value: float
    fx_to_base: float
    market_value_base: float
    unrealized_pnl: float | None
    realized_pnl: float | None


@dataclass
class AccountState:
    ts: str
    account: str
    base_currency: str
    nav_base: float
    cash_base: float
    unrealized_pnl_base: float | None
    realized_pnl_base: float | None
    fx: dict[str, float]
    positions: list[Position] = field(default_factory=list)


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # IB sends DBL_MAX (1.797e308) for "unset".
    return None if abs(f) > 1e300 else f


def parse_account(values: list[Any], items: list[Any], account: str = "") -> AccountState:
    """Turn ib_async AccountValue and PortfolioItem lists into an AccountState.

    Market values come in each contract's own currency; IB's ExchangeRate account values give
    the rate to the base currency, so no outside FX source is needed for the live view.
    """
    if account:
        values = [v for v in values if v.account == account]
        items = [i for i in items if i.account == account]
    if not values:
        raise GatewayError("gateway returned no account values (wrong IB_ACCOUNT or not synced)")
    acct = account or values[0].account

    fx: dict[str, float] = {}
    tags: dict[tuple[str, str], float] = {}
    base_currency = ""
    for v in values:
        num = _num(v.value)
        if v.tag == "ExchangeRate" and num is not None:
            fx[v.currency] = num
        elif num is not None:
            tags[(v.tag, v.currency)] = num
        if v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
            base_currency = v.currency
    if not base_currency:
        raise GatewayError("could not find the base currency (no NetLiquidation value)")
    fx.pop("BASE", None)
    fx[base_currency] = 1.0

    def tag(name: str) -> float | None:
        return tags.get((name, base_currency), tags.get((name, "BASE")))

    nav = tag("NetLiquidation")
    if nav is None:
        raise GatewayError("no NetLiquidation value")
    cash = tag("TotalCashValue")
    if cash is None:
        cash = tags.get(("CashBalance", "BASE"), 0.0)

    positions = []
    for item in items:
        if not item.position:
            continue
        c = item.contract
        rate = fx.get(c.currency)
        if rate is None:
            raise GatewayError(f"no exchange rate for {c.currency} ({c.symbol})")
        mv = _num(item.marketValue) or 0.0
        positions.append(Position(
            symbol=c.symbol,
            con_id=getattr(c, "conId", None) or None,
            exchange=getattr(c, "primaryExchange", "") or getattr(c, "exchange", ""),
            currency=c.currency,
            sec_type=getattr(c, "secType", ""),
            quantity=float(item.position),
            avg_cost=_num(item.averageCost),
            market_price=_num(item.marketPrice),
            market_value=mv,
            fx_to_base=rate,
            market_value_base=mv * rate,
            unrealized_pnl=_num(item.unrealizedPNL),
            realized_pnl=_num(item.realizedPNL),
        ))
    return AccountState(
        ts=datetime.now(UTC).replace(microsecond=0).isoformat(),
        account=acct,
        base_currency=base_currency,
        nav_base=nav,
        cash_base=cash,
        unrealized_pnl_base=tag("UnrealizedPnL"),
        realized_pnl_base=tag("RealizedPnL"),
        fx=fx,
        positions=sorted(positions, key=lambda p: -p.market_value_base),
    )


def _connect(host: str, port: int, client_id: int, account: str, timeout: float):
    from ib_async import IB  # imported lazily: the web process never talks to the gateway

    # APScheduler runs jobs in worker threads, which have no event loop of their own.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True,
                   account=account)
    except Exception as e:  # ib_async raises several unrelated types on refusal/timeout
        raise GatewayError(f"cannot reach IB Gateway at {host}:{port}: {e!r}") from e
    return ib


def fetch_account(host: str, port: int, client_id: int, account: str = "",
                  timeout: float = 20) -> AccountState:
    ib = _connect(host, port, client_id, account, timeout)
    try:
        ib.sleep(2)  # let the initial portfolio stream settle after the sync
        return parse_account(ib.accountValues(), ib.portfolio(), account)
    finally:
        ib.disconnect()


def fetch_daily_bars(host: str, port: int, client_id: int, con_ids: dict[str, int],
                     duration: str = "10 Y") -> dict[str, list[tuple[str, float]] | str]:
    """Daily closes per symbol, or an error string for symbols IB refuses (often permissions)."""
    from ib_async import Contract

    ib = _connect(host, port, client_id, "", 20)
    out: dict[str, list[tuple[str, float]] | str] = {}
    try:
        for symbol, con_id in con_ids.items():
            contract = Contract(conId=con_id)
            try:
                ib.qualifyContracts(contract)
                bars = []
                for what in ("ADJUSTED_LAST", "TRADES"):
                    bars = ib.reqHistoricalData(
                        contract, endDateTime="", durationStr=duration, barSizeSetting="1 day",
                        whatToShow=what, useRTH=True, formatDate=1, timeout=60)
                    if bars:
                        break
                if not bars:
                    out[symbol] = "IB returned no bars (market data permission or no history)"
                    continue
                out[symbol] = [(str(b.date)[:10], float(b.close)) for b in bars]
            except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the rest
                out[symbol] = f"{type(e).__name__}: {e}"
    finally:
        ib.disconnect()
    return out
