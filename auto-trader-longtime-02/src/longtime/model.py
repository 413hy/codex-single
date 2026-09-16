"""Trading signal schema only. Model execution lives in single-analysis."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}USDT$")
    decision: Literal["LONG", "SHORT", "SKIP"]
    reason: str = Field(min_length=1, max_length=600)
