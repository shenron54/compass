from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class PortfolioConfig(BaseModel):
    base_currency: str = "HKD"
    account: str = ""
    targets: dict[str, float] = Field(default_factory=dict)
    bands: dict[str, float] = Field(default_factory=lambda: {"default": 0.05})
    tech_sectors: list[str] = Field(
        default_factory=lambda: ["Information Technology", "Communication Services"])
    price_series: dict[str, str] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    source: str = "defaults"

    def band(self, symbol: str) -> float:
        return self.bands.get(symbol, self.bands.get("default", 0.05))

    def key(self, symbol: str) -> str:
        return self.aliases.get(symbol, symbol)


class EtfLookthrough(BaseModel):
    name: str
    index: str
    as_of: str
    source: str
    sectors: dict[str, float]
    names: dict[str, float]


class StockInfo(BaseModel):
    name: str
    sector: str
    same_as: str | None = None


class Lookthrough(BaseModel):
    etfs: dict[str, EtfLookthrough]
    stocks: dict[str, StockInfo]


def load_portfolio_config(local_dir: Path) -> PortfolioConfig:
    local = local_dir / "portfolio.yaml"
    if local.exists():
        text, label = local.read_text(), "local"
    else:
        text = resources.files("compass.defaults").joinpath("portfolio.example.yaml").read_text()
        label = "example"
    return PortfolioConfig(**(yaml.safe_load(text) or {}), source=label)


def load_lookthrough(local_dir: Path) -> Lookthrough:
    data = yaml.safe_load(
        resources.files("compass.defaults").joinpath("lookthrough.yaml").read_text())
    override = local_dir / "lookthrough.yaml"
    if override.exists():
        extra = yaml.safe_load(override.read_text()) or {}
        for section in ("etfs", "stocks"):
            data.setdefault(section, {}).update(extra.get(section, {}))
    return Lookthrough(**data)
