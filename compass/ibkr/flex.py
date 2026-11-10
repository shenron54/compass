"""IBKR Flex Web Service: the official record of trades, cash movements and daily NAV.

Two calls: SendRequest returns a reference code, GetStatement returns the report once IBKR has
generated it (error 1019 means "still generating", so we wait and ask again).
"""
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx

SEND_URL = ("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/"
            "SendRequest")
RETRY_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}


class FlexError(RuntimeError):
    pass


@dataclass
class FlexReport:
    trades: list[dict] = field(default_factory=list)
    cash: list[dict] = field(default_factory=list)
    nav: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})(\d{2})(\d{2})"), (1, 2, 3)),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), (1, 2, 3)),
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})"), (3, 1, 2)),
)


def parse_date(raw: str | None) -> str | None:
    """Flex dates follow the query's own format setting; accept the ones IBKR offers."""
    if not raw:
        return None
    raw = raw.strip()
    for pattern, (y, m, d) in _DATE_PATTERNS:
        match = pattern.match(raw)
        if match:
            return date(int(match.group(y)), int(match.group(m)), int(match.group(d))).isoformat()
    return None


def _f(raw: str | None) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_report(xml_text: str) -> FlexReport:
    root = ET.fromstring(xml_text)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"unexpected Flex root element {root.tag}")
    report = FlexReport()
    for el in root.iter("Trade"):
        a = el.attrib
        if a.get("levelOfDetail", "EXECUTION") not in ("EXECUTION", "ORDER", ""):
            continue
        trade_date = parse_date(a.get("tradeDate") or a.get("dateTime"))
        if not trade_date:
            continue
        report.trades.append({
            "trade_id": a.get("tradeID") or a.get("transactionID") or _digest(a),
            "trade_date": trade_date,
            "symbol": a.get("symbol", ""),
            "asset_category": a.get("assetCategory"),
            "currency": a.get("currency", ""),
            "quantity": _f(a.get("quantity")) or 0.0,
            "price": _f(a.get("tradePrice")),
            "proceeds": _f(a.get("proceeds")),
            "commission": _f(a.get("ibCommission")),
            "fx_to_base": _f(a.get("fxRateToBase")),
            "buy_sell": a.get("buySell"),
        })
    for el in root.iter("CashTransaction"):
        a = el.attrib
        when = parse_date(a.get("dateTime") or a.get("settleDate") or a.get("reportDate"))
        amount = _f(a.get("amount"))
        if not when or amount is None:
            continue
        report.cash.append({
            "transaction_id": a.get("transactionID") or _digest(a),
            "date": when,
            "type": a.get("type", ""),
            "currency": a.get("currency", ""),
            "amount": amount,
            "fx_to_base": _f(a.get("fxRateToBase")),
            "symbol": a.get("symbol") or None,
            "description": a.get("description"),
        })
    for el in root.iter("EquitySummaryByReportDateInBase"):
        a = el.attrib
        when, total = parse_date(a.get("reportDate")), _f(a.get("total"))
        if when and total is not None:
            report.nav.append({"date": when, "total_base": total, "cash_base": _f(a.get("cash")),
                               "stock_base": _f(a.get("stock"))})
    for el in root.iter("OpenPosition"):
        a = el.attrib
        report.positions.append({"symbol": a.get("symbol"), "currency": a.get("currency"),
                                 "quantity": _f(a.get("position")),
                                 "cost_basis": _f(a.get("costBasisMoney"))})
    return report


def _digest(attrs: dict) -> str:
    return hashlib.sha1(repr(sorted(attrs.items())).encode()).hexdigest()[:20]


def _status(xml_text: str) -> tuple[str, str, str, str, str]:
    root = ET.fromstring(xml_text)
    get = lambda tag: (root.findtext(tag) or "").strip()
    return (get("Status"), get("ReferenceCode"), get("Url"), get("ErrorCode"),
            get("ErrorMessage"))


def fetch_report(token: str, query_id: str, http: httpx.Client, attempts: int = 8,
                 wait: float = 10, sleep: Callable[[float], None] = time.sleep) -> FlexReport:
    if not token or not query_id:
        raise FlexError("FLEX_TOKEN and FLEX_QUERY_ID are not set")
    r = http.get(SEND_URL, params={"t": token, "q": query_id, "v": "3"})
    status, ref, url, code, message = _status(r.text)
    if status != "Success" or not ref:
        raise FlexError(f"Flex SendRequest failed ({code}): {message or r.text[:200]}")
    for _ in range(attempts):
        r = http.get(url, params={"t": token, "q": ref, "v": "3"})
        if r.text.lstrip().startswith("<FlexQueryResponse"):
            return parse_report(r.text)
        _, _, _, code, message = _status(r.text)
        if code not in RETRY_CODES:
            raise FlexError(f"Flex GetStatement failed ({code}): {message}")
        sleep(wait)
    raise FlexError("Flex statement still not ready after retries")
