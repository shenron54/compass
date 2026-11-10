from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator


class RuleBase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    note: str = ""
    enabled: bool = True


class AllocationBand(RuleBase):
    type: Literal["allocation_band"]
    measure: Literal["position_weight", "group_weight", "lookthrough_tech", "cash_weight"]
    symbols: list[str] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.min is None and self.max is None:
            raise ValueError("set min, max or both")
        if self.measure in ("position_weight", "group_weight") and not self.symbols:
            raise ValueError(f"{self.measure} needs symbols")
        return self


class ValuationFreeze(RuleBase):
    type: Literal["valuation_freeze"]
    series: str = "CAPE_PCT"
    above: float
    resume_below: float
    applies_to: str = "new purchases of equity funds"
    price_series: str = "MULTPL:SP500"

    @model_validator(mode="after")
    def _check(self):
        if self.resume_below > self.above:
            raise ValueError("resume_below must not be above 'above'")
        return self


class Tranche(BaseModel):
    drawdown: float = Field(gt=0, lt=1)
    fraction: float = Field(gt=0, le=1)
    buy: str = ""


class DrawdownLadder(RuleBase):
    type: Literal["drawdown_ladder"]
    series: str = "FRED:NASDAQ100"
    tranches: list[Tranche] = Field(min_length=1)
    budget: float | None = None
    cash_floor: float = 0.0
    reset_within: float = Field(0.03, ge=0, lt=1)

    @field_validator("tranches")
    @classmethod
    def _sorted(cls, v: list[Tranche]) -> list[Tranche]:
        v = sorted(v, key=lambda t: t.drawdown)
        if sum(t.fraction for t in v) > 1.0001:
            raise ValueError("tranche fractions add up to more than 1")
        return v


class TrimAtHigh(RuleBase):
    type: Literal["trim_at_high"]
    symbol: str
    series: str | None = None
    above_200dma: float = Field(0.20, gt=0)
    overweight_by: float = 0.0
    target: float | None = None


Rule = Annotated[AllocationBand | ValuationFreeze | DrawdownLadder | TrimAtHigh,
                 Field(discriminator="type")]
_adapter: TypeAdapter[Rule] = TypeAdapter(Rule)


def parse_rule(data: dict) -> Rule:
    return _adapter.validate_python(data)


def parse_rule_yaml(text: str) -> Rule:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise TypeError("a rule is one YAML mapping (id, name, type, ...)")
    return parse_rule(data)


def parse_rules_file(text: str) -> list[Rule]:
    data = yaml.safe_load(text) or []
    if isinstance(data, dict):
        data = data.get("rules", [])
    return [parse_rule(d) for d in data]


def dump_rule(rule: BaseModel) -> str:
    data = rule.model_dump(exclude_defaults=False, exclude_none=True)
    order = ["id", "name", "type"]
    ordered = {k: data.pop(k) for k in order if k in data} | data
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
