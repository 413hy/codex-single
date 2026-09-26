from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,24}USDT$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_.:-]{2,127}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class Candle(ContractModel):
    symbol: Symbol
    timeframe: Literal["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h"]
    open_time: datetime
    close_time: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    turnover: Decimal = Field(ge=0)
    completed: bool
    source: Literal["BYBIT", "BINANCE", "OKX"]

    @model_validator(mode="after")
    def validate_candle(self) -> Candle:
        _require_aware(self.open_time, "open_time")
        _require_aware(self.close_time, "close_time")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("low cannot exceed high")
        return self
