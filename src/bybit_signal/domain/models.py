from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    CycleStatus,
    Direction,
    MarketType,
    MonitoringMetric,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)

Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,24}USDT$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_.:-]{2,127}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class Candle(ContractModel):
    symbol: Symbol
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h"]
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


class CanonicalPrice(ContractModel):
    symbol: Symbol
    exchange: Literal["BYBIT"] = "BYBIT"
    market: Literal[MarketType.LINEAR_PERPETUAL] = MarketType.LINEAR_PERPETUAL
    price_type: PriceType
    value: Decimal = Field(gt=0)
    timestamp: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> CanonicalPrice:
        _require_aware(self.timestamp, "timestamp")
        return self


class EvidenceItem(ContractModel):
    evidence_id: EvidenceId
    category: str = Field(min_length=2, max_length=64)
    source: str = Field(min_length=2, max_length=64)
    observed_at: datetime
    summary: str = Field(min_length=2, max_length=500)
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamp(self) -> EvidenceItem:
        _require_aware(self.observed_at, "observed_at")
        return self


class EvidenceBundle(ContractModel):
    schema_version: Literal[1] = 1
    symbol: Symbol
    generated_at: datetime
    source_snapshot_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    canonical_last: CanonicalPrice
    canonical_mark: CanonicalPrice
    evidence_items: tuple[EvidenceItem, ...] = Field(min_length=1)
    tool_assessments: tuple[ToolAssessment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bundle(self) -> EvidenceBundle:
        _require_aware(self.generated_at, "generated_at")
        if self.canonical_last.symbol != self.symbol or self.canonical_mark.symbol != self.symbol:
            raise ValueError("canonical prices must match evidence bundle symbol")
        identifiers = [item.evidence_id for item in self.evidence_items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evidence identifiers must be unique")
        known = set(identifiers)
        referenced = {
            evidence_id
            for assessment in self.tool_assessments
            for evidence_id in assessment.evidence_ids
        }
        if not referenced <= known:
            raise ValueError("tool assessment references unknown evidence")
        return self


class ToolAssessment(ContractModel):
    tool: str = Field(min_length=2, max_length=80)
    status: ToolStatus
    version: str | None = Field(default=None, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    latency_ms: int | None = Field(default=None, ge=0)
    evidence_ids: tuple[EvidenceId, ...] = ()


class PriceLevel(ContractModel):
    value: Decimal = Field(gt=0)
    rationale: str = Field(min_length=4, max_length=500)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)


class CandleOutlook(ContractModel):
    direction: Direction
    strength: Literal["WEAK", "NORMAL", "STRONG"]
    window_start: datetime
    window_end: datetime
    rationale: str = Field(min_length=4, max_length=500)

    @model_validator(mode="after")
    def validate_window(self) -> CandleOutlook:
        _require_aware(self.window_start, "window_start")
        _require_aware(self.window_end, "window_end")
        if self.window_end <= self.window_start:
            raise ValueError("candle outlook window is invalid")
        return self


FormingHourOutlook = CandleOutlook


class InvalidationCondition(ContractModel):
    condition: str = Field(min_length=4, max_length=500)
    reference_price: Decimal = Field(gt=0)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)


class MonitoringDirective(ContractModel):
    family_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,95}$")]
    metric: MonitoringMetric
    comparator: Comparator
    threshold: Decimal
    hysteresis: Decimal = Field(ge=0)
    valid_for_seconds: int = Field(ge=60, le=86_400)
    reason: str = Field(min_length=4, max_length=500)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)


class CandidateAssessment(ContractModel):
    symbol: Symbol
    strength: SignalStrength
    selection_rank: Literal[1, 2] | None = None
    confidence: SignalConfidence | None = None
    direction: Direction | None = None
    market_state: str = Field(min_length=2, max_length=500)
    take_profit: PriceLevel | None = None
    forming_15m: CandleOutlook | None = None
    forming_30m: CandleOutlook | None = None
    forming_1h: CandleOutlook | None = None
    next_15m: CandleOutlook | None = None
    invalidation: InvalidationCondition | None = None
    summary: str = Field(min_length=4, max_length=1500)
    details: str = Field(min_length=4, max_length=12_000)
    uncertainties: tuple[str, ...] = ()
    evidence_ids: tuple[EvidenceId, ...] = ()
    monitoring_directives: tuple[MonitoringDirective, ...] = ()

    @model_validator(mode="after")
    def validate_strength_contract(self) -> CandidateAssessment:
        if self.selection_rank is None and self.confidence is not None:
            raise ValueError("non-selected assessment cannot have confidence")
        if self.selection_rank is not None and self.confidence is None:
            raise ValueError("selected assessment requires confidence")
        if self.strength is SignalStrength.STRONG or self.selection_rank is not None:
            if self.direction is None:
                raise ValueError("selected or strong assessment requires one direction")
            if self.take_profit is None:
                raise ValueError("selected or strong assessment requires one take-profit level")
            if self.forming_1h is None:
                raise ValueError("selected or strong assessment requires forming 1h outlook")
            if self.invalidation is None:
                raise ValueError("selected or strong assessment requires invalidation condition")
            if not self.evidence_ids:
                raise ValueError("selected or strong assessment requires evidence")
        return self


class ModelAnalysisResponse(ContractModel):
    schema_version: Literal[1] = 1
    mode: CycleMode
    analysis_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{8,96}$")]
    cycle_summary: str = Field(min_length=4, max_length=2000)
    assessments: tuple[CandidateAssessment, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_response(self) -> ModelAnalysisResponse:
        symbols = [assessment.symbol for assessment in self.assessments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("model response contains duplicate symbols")
        ranks = [
            assessment.selection_rank
            for assessment in self.assessments
            if assessment.selection_rank is not None
        ]
        if len(ranks) != len(set(ranks)):
            raise ValueError("model response contains duplicate selection ranks")
        strong_count = sum(
            assessment.strength is SignalStrength.STRONG for assessment in self.assessments
        )
        if strong_count > 2:
            raise ValueError("model response contains more than two strong signals")
        return self


class SignalConclusion(ContractModel):
    analysis_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{8,96}$")]
    generated_at: datetime
    canonical_price: CanonicalPrice
    assessment: CandidateAssessment
    tracking_status: TrackingStatus
    comparison_with_previous: str = Field(min_length=2, max_length=1000)
    tool_assessments: tuple[ToolAssessment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_signal(self) -> SignalConclusion:
        _require_aware(self.generated_at, "generated_at")
        if self.assessment.symbol != self.canonical_price.symbol:
            raise ValueError("assessment and canonical price symbols differ")
        known = {evidence_id for tool in self.tool_assessments for evidence_id in tool.evidence_ids}
        if not set(self.assessment.evidence_ids) <= known:
            raise ValueError("assessment references unknown evidence")
        return self


class AnalysisCycleResult(ContractModel):
    selection_contract_version: Literal[1, 2, 3] = 1
    analysis_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{8,96}$")]
    mode: CycleMode = CycleMode.SCHEDULED
    status: CycleStatus = CycleStatus.SUCCESS
    started_at: datetime
    completed_at: datetime
    candidate_symbols: tuple[Symbol, ...] = Field(max_length=5)
    tracked_symbols: tuple[Symbol, ...] = ()
    conclusions: tuple[SignalConclusion, ...]
    strong_signal_count: int = Field(ge=0, le=2)
    selected_symbols: tuple[Symbol, ...] = Field(default=(), max_length=2)
    selected_signal_count: int = Field(default=0, ge=0, le=2)
    context_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_cycle(self) -> AnalysisCycleResult:
        _require_aware(self.started_at, "started_at")
        _require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("cycle completion precedes start")
        actual = sum(
            conclusion.assessment.strength is SignalStrength.STRONG
            for conclusion in self.conclusions
        )
        if self.strong_signal_count != actual:
            raise ValueError("strong_signal_count does not match conclusions")
        selected = sorted(
            (
                conclusion
                for conclusion in self.conclusions
                if conclusion.assessment.selection_rank is not None
            ),
            key=lambda conclusion: conclusion.assessment.selection_rank or 0,
        )
        selected_symbols = tuple(
            conclusion.assessment.symbol for conclusion in selected
        )
        if self.selected_signal_count != len(selected):
            raise ValueError("selected_signal_count does not match conclusions")
        if self.selected_symbols != selected_symbols:
            raise ValueError("selected_symbols do not match ranked conclusions")
        if self.selection_contract_version == 2:
            if self.mode is CycleMode.SCHEDULED and self.status is CycleStatus.SUCCESS:
                if self.selected_signal_count != 2:
                    raise ValueError("successful scheduled cycle requires two selected signals")
            elif self.selected_signal_count != 0:
                raise ValueError("failed or emergency cycle cannot select scheduled signals")
        if self.selection_contract_version == 3:
            if self.mode is CycleMode.SCHEDULED and self.status is CycleStatus.SUCCESS:
                if self.selected_signal_count != 2:
                    raise ValueError("successful scheduled cycle requires two selected signals")
                for conclusion in self.conclusions:
                    assessment = conclusion.assessment
                    if assessment.selection_rank is not None:
                        _require_all_outlooks(assessment)
                    elif any(
                        outlook is not None
                        for outlook in (
                            assessment.forming_15m,
                            assessment.forming_30m,
                            assessment.forming_1h,
                            assessment.next_15m,
                        )
                    ):
                        raise ValueError("non-selected assessment cannot contain new outlooks")
            elif self.mode is CycleMode.EMERGENCY and self.status is CycleStatus.SUCCESS:
                if self.selected_signal_count != 0:
                    raise ValueError("emergency cycle cannot select scheduled signals")
                if len(self.conclusions) != 1:
                    raise ValueError("successful emergency cycle requires one conclusion")
                _require_all_outlooks(self.conclusions[0].assessment)
            elif self.selected_signal_count != 0:
                raise ValueError("failed cycle cannot select scheduled signals")
        return self


def _require_all_outlooks(assessment: CandidateAssessment) -> None:
    if any(
        outlook is None
        for outlook in (
            assessment.forming_15m,
            assessment.forming_30m,
            assessment.forming_1h,
            assessment.next_15m,
        )
    ):
        raise ValueError("visible signal requires all four candle outlooks")
