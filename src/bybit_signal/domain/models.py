from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from bybit_signal.domain.enums import (
    CandidateRiskTag,
    Comparator,
    CycleMode,
    CycleStatus,
    Direction,
    FailureStep,
    FailureWorkflow,
    FreshnessDecision,
    MarketType,
    MonitoringMetric,
    MonitoringReviewDecision,
    PriceType,
    RetryAction,
    RetryStatus,
    SignalConfidence,
    SignalStrength,
    ThresholdSeverity,
    ToolStatus,
    ToolTurnAction,
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
    values: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamp(self) -> EvidenceItem:
        _require_aware(self.observed_at, "observed_at")
        return self


class EvidenceBundle(ContractModel):
    schema_version: Literal[1, 2] = 2
    symbol: Symbol
    generated_at: datetime
    source_snapshot_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    canonical_last: CanonicalPrice
    canonical_mark: CanonicalPrice
    evidence_items: tuple[EvidenceItem, ...] = Field(min_length=1)
    tool_assessments: tuple[ToolAssessment, ...] = Field(min_length=1)
    market_context: MarketContext | None = None
    candidate_context: CandidateContext | None = None

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


class MonitoringCondition(ContractModel):
    metric: MonitoringMetric
    comparator: Comparator
    threshold: Decimal
    current_value: Decimal
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    confirmation: str = Field(min_length=3, max_length=80)


class MonitoringDirective(ContractModel):
    family_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,95}$")]
    metric: MonitoringMetric
    comparator: Comparator
    threshold: Decimal
    hysteresis: Decimal = Field(ge=0)
    valid_for_seconds: int = Field(ge=60, le=86_400)
    reason: str = Field(min_length=4, max_length=500)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1)
    severity: ThresholdSeverity = ThresholdSeverity.IMPORTANT
    current_value: Decimal | None = None
    distance_percent: Decimal | None = Field(default=None, ge=0)
    distance_atr: Decimal | None = Field(default=None, ge=0)
    confirmation: str = Field(default="realtime_cross", min_length=3, max_length=80)
    confirmation_seconds: int = Field(default=0, ge=0, le=30)
    required_consecutive_observations: int = Field(default=1, ge=1, le=3)
    confirmations: tuple[MonitoringCondition, ...] = Field(default=(), max_length=2)


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


class MonitoringRuleReview(ContractModel):
    symbol: Symbol
    decision: MonitoringReviewDecision
    rationale: str = Field(min_length=4, max_length=1500)
    directives: tuple[MonitoringDirective, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_decision(self) -> MonitoringRuleReview:
        if self.decision is MonitoringReviewDecision.REJECTED and self.directives:
            raise ValueError("rejected monitoring review cannot contain directives")
        if self.decision is not MonitoringReviewDecision.REJECTED and not self.directives:
            raise ValueError("accepted monitoring review requires directives")
        return self


class ModelMonitoringReviewResponse(ContractModel):
    schema_version: Literal[1] = 1
    analysis_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{8,96}$")]
    reviews: tuple[MonitoringRuleReview, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_reviews(self) -> ModelMonitoringReviewResponse:
        symbols = [review.symbol for review in self.reviews]
        if len(symbols) != len(set(symbols)):
            raise ValueError("monitoring review contains duplicate symbols")
        return self


class BreadthSnapshot(ContractModel):
    observed_at: datetime
    instrument_count: int = Field(ge=0)
    advancing_count: int = Field(ge=0)
    declining_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    median_change_24h_percent: float | None = None
    positive_turnover_share: float | None = Field(default=None, ge=0, le=1)
    top_turnover_share: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_breadth(self) -> BreadthSnapshot:
        _require_aware(self.observed_at, "observed_at")
        if (
            self.advancing_count + self.declining_count + self.unchanged_count
            != self.instrument_count
        ):
            raise ValueError("breadth counts do not match instrument_count")
        return self


class MarketContext(ContractModel):
    schema_version: Literal[1] = 1
    generated_at: datetime
    breadth: BreadthSnapshot
    btc_returns_percent: dict[str, float | None] = Field(default_factory=dict)
    eth_returns_percent: dict[str, float | None] = Field(default_factory=dict)
    candidate_median_return_5m_percent: float | None = None
    candidate_synchronization: float | None = Field(default=None, ge=-1, le=1)
    liquidation_status: ToolStatus = ToolStatus.WARMING_UP
    liquidation_notional_5m: Decimal | None = Field(default=None, ge=0)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_market_context(self) -> MarketContext:
        _require_aware(self.generated_at, "generated_at")
        return self


class CandidateContext(ContractModel):
    rank: int = Field(ge=1, le=10)
    opportunity_score: float = Field(ge=0, le=100)
    tradability_score: float = Field(ge=0, le=100)
    final_score: float = Field(ge=0, le=100)
    risk_tags: tuple[CandidateRiskTag, ...] = ()
    raw_features: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class AnalysisToolArguments(ContractModel):
    timeframes: str | None = Field(default=None, max_length=32)
    limit: int | None = Field(default=None, ge=1, le=240)


class AnalysisToolRequest(ContractModel):
    request_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{3,64}$")]
    tool: Literal[
        "latest_market",
        "short_candles",
        "market_context",
        "depth_and_trades",
        "derivatives",
        "cross_exchange",
        "signal_history",
        "extended_candles",
    ]
    symbol: Symbol | None = None
    arguments: AnalysisToolArguments = AnalysisToolArguments()
    reason: str = Field(min_length=4, max_length=500)


class AnalysisToolResult(ContractModel):
    request_id: str
    tool: str
    symbol: Symbol | None = None
    status: ToolStatus
    requested_at: datetime
    completed_at: datetime
    latency_ms: int = Field(ge=0)
    evidence_items: tuple[EvidenceItem, ...] = ()
    error: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_tool_result(self) -> AnalysisToolResult:
        _require_aware(self.requested_at, "requested_at")
        _require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.requested_at:
            raise ValueError("tool result completes before request")
        return self


class ModelTurnResponse(ContractModel):
    schema_version: Literal[1] = 1
    action: ToolTurnAction
    requests: tuple[AnalysisToolRequest, ...] = Field(default=(), max_length=8)
    final: ModelAnalysisResponse | None = None

    @model_validator(mode="after")
    def validate_action(self) -> ModelTurnResponse:
        if self.action is ToolTurnAction.TOOL_REQUESTS:
            if not self.requests or self.final is not None:
                raise ValueError("TOOL_REQUESTS requires requests and no final")
        elif self.requests or self.final is None:
            raise ValueError("FINAL requires final and no requests")
        return self


class FreshnessAssessment(ContractModel):
    symbol: Symbol
    decision: FreshnessDecision
    checked_at: datetime
    original_price: Decimal = Field(gt=0)
    latest_price: Decimal = Field(gt=0)
    drift_percent: Decimal
    drift_atr_1m: Decimal | None = None
    invalidation_buffer_consumed: Decimal | None = Field(default=None, ge=0)
    spread_bps: Decimal = Field(ge=0)
    depth_retention: Decimal | None = Field(default=None, ge=0)
    book_available: bool
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_freshness(self) -> FreshnessAssessment:
        _require_aware(self.checked_at, "checked_at")
        return self


class SignalOutcome(ContractModel):
    analysis_id: str
    symbol: Symbol
    evaluated_at: datetime
    signal_time: datetime
    direction: Direction
    reference_price: Decimal = Field(gt=0)
    latest_price: Decimal = Field(gt=0)
    # Nullable compatibility field for historical rows. The current system does not
    # grade or act on the display-only approximate take-profit.
    target_touched: bool | None = None
    invalidation_touched: bool
    mfe_percent: Decimal
    mae_percent: Decimal
    next_15m_correct: bool | None = None
    forming_15m_correct: bool | None = None
    forming_30m_correct: bool | None = None
    forming_1h_correct: bool | None = None
    error_type: str | None = Field(default=None, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_outcome(self) -> SignalOutcome:
        _require_aware(self.evaluated_at, "evaluated_at")
        _require_aware(self.signal_time, "signal_time")
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
    selection_contract_version: Literal[1, 2, 3, 4] = 1
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
        selected_symbols = tuple(conclusion.assessment.symbol for conclusion in selected)
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
        if self.selection_contract_version in {3, 4}:
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


class FailureEvent(ContractModel):
    failure_id: Annotated[str, StringConstraints(pattern=r"^fail_[a-zA-Z0-9_-]{8,80}$")]
    analysis_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{8,96}$")]
    occurred_at: datetime
    workflow: FailureWorkflow
    step: FailureStep
    step_index: int = Field(ge=1)
    total_steps: int = Field(ge=1)
    completed_steps: tuple[str, ...] = ()
    code: str = Field(min_length=2, max_length=120)
    symbol: Symbol | None = None
    direct_cause: str = Field(min_length=4, max_length=1500)
    causal_chain: tuple[str, ...] = Field(min_length=1, max_length=8)
    impact: str = Field(min_length=4, max_length=1500)
    resolution: str = Field(min_length=4, max_length=1500)
    retry_action: RetryAction
    safe_diagnostics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_failure(self) -> FailureEvent:
        _require_aware(self.occurred_at, "occurred_at")
        if self.step_index > self.total_steps:
            raise ValueError("failure step_index exceeds total_steps")
        return self


class RetryJob(ContractModel):
    token: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{10,24}$")]
    failure_id: str
    status: RetryStatus
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    requested_by: int | None = None
    result_analysis_id: str | None = None
    error: str | None = Field(default=None, max_length=1500)

    @model_validator(mode="after")
    def validate_retry_job(self) -> RetryJob:
        _require_aware(self.created_at, "created_at")
        if self.claimed_at is not None:
            _require_aware(self.claimed_at, "claimed_at")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
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
