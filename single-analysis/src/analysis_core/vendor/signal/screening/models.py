from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from analysis_core.vendor.signal.domain.enums import CycleStatus
from analysis_core.vendor.signal.domain.models import ContractModel, EvidenceId, Symbol


class ScreeningAssessmentStatus(StrEnum):
    SELECTED = "SELECTED"
    NOT_SELECTED = "NOT_SELECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ScreeningValue(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    UNDETERMINED = "UNDETERMINED"


class ScreeningReasonCategory(StrEnum):
    ACTIVITY = "ACTIVITY"
    STRUCTURE_CLARITY = "STRUCTURE_CLARITY"
    PARTICIPATION = "PARTICIPATION"
    TRADABILITY = "TRADABILITY"
    DISTINCTIVENESS = "DISTINCTIVENESS"
    DATA_QUALITY = "DATA_QUALITY"


class ScreeningRiskCategory(StrEnum):
    THIN_LIQUIDITY = "THIN_LIQUIDITY"
    WIDE_SPREAD = "WIDE_SPREAD"
    CHOPPY = "CHOPPY"
    IMPACT_DISTORTION = "IMPACT_DISTORTION"
    WEAK_PARTICIPATION = "WEAK_PARTICIPATION"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    MARKET_BETA_DOMINATED = "MARKET_BETA_DOMINATED"
    STALE_OR_MISSING_DATA = "STALE_OR_MISSING_DATA"
    OTHER = "OTHER"


class EvidenceBackedReason(ContractModel):
    category: ScreeningReasonCategory
    statement: str = Field(min_length=4, max_length=400)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=3)


class EvidenceBackedRisk(ContractModel):
    category: ScreeningRiskCategory
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    statement: str = Field(min_length=4, max_length=400)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=3)


class CandidateScreeningAssessment(ContractModel):
    symbol: Symbol
    status: ScreeningAssessmentStatus
    selection_rank: Literal[1, 2, 3] | None = None
    value: ScreeningValue
    value_summary: str = Field(min_length=4, max_length=800)
    reasons: tuple[EvidenceBackedReason, ...] = Field(min_length=1, max_length=4)
    risks: tuple[EvidenceBackedRisk, ...] = Field(min_length=1, max_length=4)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=4)
    ranking_rationale: str = Field(min_length=4, max_length=800)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_selection_contract(self) -> CandidateScreeningAssessment:
        if self.status is ScreeningAssessmentStatus.SELECTED:
            if self.selection_rank is None:
                raise ValueError("selected screening assessment requires selection_rank")
            if self.value not in {ScreeningValue.HIGH, ScreeningValue.MEDIUM}:
                raise ValueError("selected screening assessment requires HIGH or MEDIUM value")
            categories = {reason.category for reason in self.reasons}
            market_categories = categories - {ScreeningReasonCategory.DATA_QUALITY}
            if ScreeningReasonCategory.STRUCTURE_CLARITY not in categories:
                raise ValueError("selected screening assessment requires STRUCTURE_CLARITY")
            if len(market_categories) < 2:
                raise ValueError(
                    "selected screening assessment requires two non-data-quality reason categories"
                )
            if any(
                risk.severity == "HIGH"
                and risk.category
                in {
                    ScreeningRiskCategory.THIN_LIQUIDITY,
                    ScreeningRiskCategory.WIDE_SPREAD,
                }
                for risk in self.risks
            ):
                raise ValueError("selected screening assessment cannot carry HIGH execution risk")
        elif self.selection_rank is not None:
            raise ValueError("non-selected screening assessment cannot have selection_rank")
        elif self.value is not ScreeningValue.UNDETERMINED:
            raise ValueError("non-selected screening assessment requires UNDETERMINED value")
        if not self.risks:
            raise ValueError("screening assessment requires at least one risk")
        cited = set(self.evidence_ids)
        nested = {evidence_id for reason in self.reasons for evidence_id in reason.evidence_ids} | {
            evidence_id for risk in self.risks for evidence_id in risk.evidence_ids
        }
        if not nested <= cited:
            raise ValueError("nested screening evidence must be listed in evidence_ids")
        return self


class ModelScreeningResponse(ContractModel):
    schema_version: Literal[1] = 1
    contract_kind: Literal["MARKET_SCREENING"] = "MARKET_SCREENING"
    analysis_id: str = Field(min_length=8, max_length=96)
    mode: Literal["SCHEDULED"] = "SCHEDULED"
    selection_summary: str = Field(min_length=4, max_length=1500)
    assessments: tuple[CandidateScreeningAssessment, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def validate_response(self) -> ModelScreeningResponse:
        symbols = [item.symbol for item in self.assessments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("screening response contains duplicate symbols")
        selected = sorted(
            (item for item in self.assessments if item.selection_rank is not None),
            key=lambda item: item.selection_rank or 0,
        )
        ranks = [item.selection_rank for item in selected]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("screening ranks must be contiguous from one and at most three")
        if len(selected) > 3:
            raise ValueError("screening response contains more than three selected candidates")
        return self


class ScreeningConclusion(ContractModel):
    analysis_id: str = Field(min_length=8, max_length=96)
    generated_at: datetime
    assessment: CandidateScreeningAssessment

    @model_validator(mode="after")
    def validate_conclusion(self) -> ScreeningConclusion:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("screening conclusion generated_at must be timezone-aware")
        if self.assessment.symbol == "":
            raise ValueError("screening conclusion symbol cannot be empty")
        return self


class ScreeningCycleResult(ContractModel):
    contract_version: Literal[1] = 1
    analysis_id: str = Field(min_length=8, max_length=96)
    mode: Literal["SCHEDULED"] = "SCHEDULED"
    status: CycleStatus = CycleStatus.SUCCESS
    started_at: datetime
    completed_at: datetime
    candidate_symbols: tuple[Symbol, ...] = Field(min_length=0, max_length=6)
    conclusions: tuple[ScreeningConclusion, ...] = Field(min_length=0, max_length=6)
    selected_symbols: tuple[Symbol, ...] = Field(max_length=3)
    selected_count: int = Field(ge=0, le=3)
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    diagnostics: dict[str, object] = Field(default_factory=dict)

    @property
    def selected_signal_count(self) -> int:
        """Compatibility name used by scheduler logging during migration."""

        return self.selected_count

    @model_validator(mode="after")
    def validate_cycle(self) -> ScreeningCycleResult:
        if self.completed_at < self.started_at:
            raise ValueError("screening cycle completion precedes start")
        if self.status is CycleStatus.SUCCESS and (
            len(self.candidate_symbols) != 6 or len(self.conclusions) != 6
        ):
            raise ValueError("successful screening cycle requires exactly six candidates")
        if tuple(item.assessment.symbol for item in self.conclusions) != self.candidate_symbols:
            raise ValueError("screening conclusions must follow candidate symbol order")
        selected = sorted(
            (
                item.assessment
                for item in self.conclusions
                if item.assessment.selection_rank is not None
            ),
            key=lambda item: item.selection_rank or 0,
        )
        selected_symbols = tuple(item.symbol for item in selected)
        if selected_symbols != self.selected_symbols or len(selected) != self.selected_count:
            raise ValueError("screening selected fields do not match conclusions")
        return self


# Short alias used by notification and integration layers.
ScreeningAssessment = CandidateScreeningAssessment
