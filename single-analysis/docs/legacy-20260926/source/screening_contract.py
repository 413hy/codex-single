"""Ten-candidate relative trend ranking; minimum one directional recommendation."""

from typing import Literal

from pydantic import Field, model_validator

from analysis_core.vendor.signal.domain.models import ContractModel, EvidenceId, Symbol
from analysis_core.vendor.signal.screening.models import EvidenceBackedReason, EvidenceBackedRisk


class TrendAssessment(ContractModel):
    symbol: Symbol
    status: Literal["SELECTED", "NOT_SELECTED", "INSUFFICIENT_DATA"]
    selection_rank: Literal[1, 2, 3] | None
    direction: Literal["LONG", "SHORT", "SKIP"]
    value: Literal["HIGH", "MEDIUM", "LOW", "UNDETERMINED"]
    value_summary: str = Field(min_length=4, max_length=800)
    reasons: tuple[EvidenceBackedReason, ...] = Field(min_length=1, max_length=4)
    risks: tuple[EvidenceBackedRisk, ...] = Field(min_length=1, max_length=4)
    uncertainties: tuple[str, ...] = Field(max_length=4)
    ranking_rationale: str = Field(min_length=4, max_length=800)
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def check_assessment(self):
        if self.status == "SELECTED":
            if (
                self.selection_rank is None
                or self.direction == "SKIP"
                or self.value == "UNDETERMINED"
            ):
                raise ValueError(
                    "Selected trend candidate requires rank, direction and honest confidence"
                )
            if not any(r.category == "STRUCTURE_CLARITY" for r in self.reasons):
                raise ValueError("Selected trend candidate requires structural evidence")
        elif self.selection_rank is not None:
            raise ValueError("Unselected trend candidate cannot have a rank")
        if self.status == "INSUFFICIENT_DATA" and (
            self.direction != "SKIP" or self.value != "UNDETERMINED"
        ):
            raise ValueError("Insufficient data cannot imply a directional conclusion")
        nested = {eid for r in self.reasons for eid in r.evidence_ids} | {
            eid for r in self.risks for eid in r.evidence_ids
        }
        if not nested <= set(self.evidence_ids):
            raise ValueError("Nested citations must be present in evidence index")
        return self


class TrendScreeningResponse(ContractModel):
    schema_version: Literal[2]
    contract_kind: Literal["MARKET_SCREENING"]
    analysis_id: str = Field(min_length=8, max_length=96)
    mode: Literal["SCHEDULED"]
    selection_summary: str = Field(min_length=4, max_length=1500)
    assessments: tuple[TrendAssessment, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def check_selection(self):
        if len({a.symbol for a in self.assessments}) != 10:
            raise ValueError("Ten distinct symbols are required")
        ranks = sorted(a.selection_rank for a in self.assessments if a.selection_rank is not None)
        if not 1 <= len(ranks) <= 3 or ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("Select one to three candidates with contiguous ranks")
        return self
