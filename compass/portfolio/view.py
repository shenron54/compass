import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from compass.portfolio.config import Lookthrough, PortfolioConfig


@dataclass
class Holding:
    symbol: str
    name: str
    kind: str
    sector: str
    quantity: float
    currency: str
    market_price: float | None
    avg_cost: float | None
    market_value: float
    fx_to_base: float
    value_base: float
    weight: float
    target: float | None
    band: float
    unrealized_pnl_base: float | None
    con_id: int | None = None

    @property
    def drift(self) -> float | None:
        return None if self.target is None else self.weight - self.target

    @property
    def status(self) -> str:
        if self.target is None:
            return "untargeted"
        if self.weight > self.target + self.band:
            return "over"
        if self.weight < self.target - self.band:
            return "under"
        return "ok"


@dataclass
class Exposure:
    key: str
    name: str
    direct: float
    via_funds: float

    @property
    def total(self) -> float:
        return self.direct + self.via_funds


@dataclass
class PortfolioView:
    ts: str
    source: str
    account: str
    base_currency: str
    nav: float
    cash: float
    cash_target: float | None
    cash_band: float
    unrealized_pnl_base: float | None
    holdings: list[Holding]
    sectors: list[tuple[str, float]]
    tech_weight: float
    tech_sectors: list[str]
    names: list[Exposure]
    lookthrough_as_of: dict[str, str]
    config_source: str
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def cash_weight(self) -> float:
        return self.cash / self.nav if self.nav else 0.0

    @property
    def cash_status(self) -> str:
        if self.cash_target is None:
            return "untargeted"
        if self.cash_weight > self.cash_target + self.cash_band:
            return "over"
        if self.cash_weight < self.cash_target - self.cash_band:
            return "under"
        return "ok"

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return (now - datetime.fromisoformat(self.ts)).total_seconds() / 60


def build_view(snapshot: dict, positions: list[dict], cfg: PortfolioConfig,
               lt: Lookthrough) -> PortfolioView:
    nav = snapshot["nav_base"]
    holdings: list[Holding] = []
    sectors: dict[str, float] = defaultdict(float)
    names: dict[str, Exposure] = {}
    as_of: dict[str, str] = {}

    def exposure(key: str) -> Exposure:
        stock = lt.stocks.get(key)
        key = stock.same_as if stock and stock.same_as else key
        if key not in names:
            info = lt.stocks.get(key)
            names[key] = Exposure(key, info.name if info else key, 0.0, 0.0)
        return names[key]

    for p in positions:
        key = cfg.key(p["symbol"])
        weight = p["market_value_base"] / nav if nav else 0.0
        etf = lt.etfs.get(key)
        stock = lt.stocks.get(key)
        if etf:
            kind, sector, name = "etf", "Fund", etf.name
            as_of[key] = etf.as_of
            for s, w in etf.sectors.items():
                sectors[s] += weight * w
            for n, w in etf.names.items():
                exposure(n).via_funds += weight * w
        elif stock:
            kind, sector, name = "stock", stock.sector, stock.name
            sectors[stock.sector] += weight
            exposure(key).direct += weight
        else:
            kind, sector, name = "other", "Unclassified", key
            sectors["Unclassified"] += weight
        fx = p["fx_to_base"]
        upnl = p.get("unrealized_pnl")
        holdings.append(Holding(
            symbol=p["symbol"], name=name, kind=kind, sector=sector, quantity=p["quantity"],
            currency=p["currency"], market_price=p.get("market_price"),
            avg_cost=p.get("avg_cost"), market_value=p["market_value"], fx_to_base=fx,
            value_base=p["market_value_base"], weight=weight, target=cfg.targets.get(key),
            band=cfg.band(key), unrealized_pnl_base=None if upnl is None else upnl * fx,
            con_id=p.get("con_id"),
        ))
    cash = snapshot["cash_base"]
    if nav:
        sectors["Cash"] += cash / nav
    tech = sum(w for s, w in sectors.items() if s in cfg.tech_sectors)
    holdings.sort(key=lambda h: -h.value_base)
    return PortfolioView(
        ts=snapshot["ts"], source=snapshot["source"], account=snapshot["account"],
        base_currency=snapshot["base_currency"], nav=nav, cash=cash,
        cash_target=cfg.targets.get("CASH"), cash_band=cfg.band("CASH"),
        unrealized_pnl_base=snapshot.get("unrealized_pnl_base"), holdings=holdings,
        sectors=sorted(sectors.items(), key=lambda kv: -kv[1]), tech_weight=tech,
        tech_sectors=cfg.tech_sectors,
        names=sorted(names.values(), key=lambda e: -e.total), lookthrough_as_of=as_of,
        config_source=cfg.source,
        weights={h.symbol: h.weight for h in holdings} | {"CASH": cash / nav if nav else 0.0},
    )


def latest_snapshot(conn: sqlite3.Connection) -> tuple[dict, list[dict]] | None:
    row = conn.execute("SELECT * FROM snapshots ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    positions = conn.execute(
        "SELECT * FROM snapshot_positions WHERE snapshot_id = ? ORDER BY market_value_base DESC",
        (row["id"],),
    ).fetchall()
    return dict(row), [dict(p) for p in positions]


def load_view(conn: sqlite3.Connection, cfg: PortfolioConfig,
              lt: Lookthrough) -> PortfolioView | None:
    latest = latest_snapshot(conn)
    if latest is None:
        return None
    return build_view(latest[0], latest[1], cfg, lt)
