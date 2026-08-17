from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from bybit_signal.domain.enums import ToolStatus
from bybit_signal.domain.models import ContractModel, Symbol


class CmiSnapshot(ContractModel):
    """A newly generated, identity-checked CMI snapshot.

    The raw payload is retained for evidence extraction, but availability and
    provenance are made explicit so missing exchanges never become zero-valued
    observations.
    """

    symbol: Symbol
    schema_version: Literal["2.0"]
    generated_at: datetime
    captured_at: datetime
    json_path: Path
    text_path: Path
    health_path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal[ToolStatus.AVAILABLE, ToolStatus.PARTIAL]
    snapshot_status: str = Field(min_length=2, max_length=120)
    completeness_status: str = Field(min_length=2, max_length=120)
    health_status: str = Field(min_length=2, max_length=120)
    application_version: str = Field(min_length=1, max_length=120)
    available_perpetual_exchanges: tuple[str, ...]
    limitation_count: int = Field(ge=0)
    limitation_summaries: tuple[str, ...] = Field(max_length=20)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_snapshot(self) -> CmiSnapshot:
        for field, value in (
            ("generated_at", self.generated_at),
            ("captured_at", self.captured_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        if "bybit" not in self.available_perpetual_exchanges:
            raise ValueError("CMI snapshot requires Bybit perpetual evidence")
        return self
