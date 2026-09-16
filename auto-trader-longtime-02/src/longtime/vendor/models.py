from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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


class CandidateRiskTag(StrEnum):
    THIN_LIQUIDITY = "THIN_LIQUIDITY"
    CHOPPY = "CHOPPY"
    IMPACT_EXTENDED = "IMPACT_EXTENDED"
    DATA_DEGRADED = "DATA_DEGRADED"


class ScannerConfig(BaseModel):
    preselect_limit: int = Field(default=60, ge=5, le=200)
    candle_limit: int = Field(default=72, ge=49, le=300)
    request_concurrency: int = Field(default=8, ge=1, le=20)
    minimum_24h_turnover_usdt: float = Field(default=1_500_000, ge=0)
    minimum_recent_30m_turnover_usdt: float = Field(default=50_000, ge=0)
    maximum_spread_bps: float = Field(default=25, gt=0, le=500)
    minimum_completed_candles: int = Field(default=48, ge=24, le=200)
    maximum_missing_intervals: int = Field(default=2, ge=0, le=12)
    minimum_median_range_percent: float = Field(default=0.15, ge=0, le=100)
    minimum_max_return_percent: float = Field(default=0.35, ge=0, le=100)
    depth_check_limit: int = Field(default=20, ge=5, le=60)
    thin_depth_notional_usdt: float = Field(default=5_000, ge=0)
    choppy_overlap_ratio: float = Field(default=0.72, ge=0, le=1)
    impact_extension_atr: float = Field(default=2.5, gt=0, le=20)
    minimum_tradability_score: float = Field(default=60, ge=0, le=100)
