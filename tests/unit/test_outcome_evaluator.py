from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.domain.enums import (
    Direction,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    Candle,
    CanonicalPrice,
    FormingHourOutlook,
    InvalidationCondition,
    PriceLevel,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.evaluation.outcomes import _evaluate


def _conclusion() -> SignalConclusion:
    generated = datetime(2026, 8, 18, 8, 0, 30, tzinfo=UTC)
    evidence_id = "ACUUSDT.PA.1M"
    invalidation_evidence_id = "ACUUSDT.PA.5M"
    return SignalConclusion(
        analysis_id="analysis_01",
        generated_at=generated,
        canonical_price=CanonicalPrice(
            symbol="ACUUSDT",
            price_type=PriceType.LAST,
            value=Decimal("100"),
            timestamp=generated,
        ),
        assessment=CandidateAssessment(
            symbol="ACUUSDT",
            strength=SignalStrength.STRONG,
            selection_rank=1,
            confidence=SignalConfidence.MEDIUM,
            direction=Direction.LONG_BIAS,
            market_state="test long structure",
            take_profit=PriceLevel(
                value=Decimal("102"),
                rationale="near structure",
                evidence_ids=(evidence_id,),
            ),
            forming_1h=FormingHourOutlook(
                direction=Direction.LONG_BIAS,
                strength="NORMAL",
                window_start=generated.replace(minute=0, second=0, microsecond=0),
                window_end=generated.replace(minute=0, second=0, microsecond=0)
                + timedelta(hours=1),
                rationale="test forming hour",
            ),
            invalidation=InvalidationCondition(
                condition="completed 5m close loses structure",
                reference_price=Decimal("98"),
                evidence_ids=(invalidation_evidence_id,),
            ),
            summary="test signal",
            details="test signal details",
            evidence_ids=(evidence_id,),
            monitoring_directives=(),
        ),
        tracking_status=TrackingStatus.NEW,
        comparison_with_previous="new test signal",
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence",
                evidence_ids=(evidence_id, invalidation_evidence_id),
            ),
        ),
    )


def _candle(open_time: datetime, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        symbol="ACUUSDT",
        timeframe="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
        turnover=Decimal("10000"),
        completed=True,
        source="BYBIT",
    )


def test_outcome_excludes_partial_signal_minute_from_mfe_and_mae() -> None:
    conclusion = _conclusion()
    minute = conclusion.generated_at.replace(second=0, microsecond=0)
    candles = (
        _candle(minute, "100", "110", "90", "100"),
        _candle(minute + timedelta(minutes=1), "100", "101", "99", "100.5"),
        _candle(minute + timedelta(minutes=2), "100.5", "102.5", "100", "102"),
    )

    outcome = _evaluate(
        conclusion,
        candles,
        evaluated_at=minute + timedelta(minutes=3),
    )

    assert outcome is not None
    assert outcome.target_touched is None
    assert outcome.invalidation_touched is False
    assert outcome.mfe_percent == Decimal("2.500000")
    assert outcome.mae_percent == Decimal("-1.000000")
    assert outcome.details["candle_count"] == 2
    assert "legacy_target_touch_at" not in outcome.details
    assert outcome.error_type is None


def test_outcome_does_not_use_intrabar_wick_as_direction_invalidation() -> None:
    conclusion = _conclusion()
    minute = conclusion.generated_at.replace(second=0, microsecond=0)
    candles = (
        _candle(minute + timedelta(minutes=1), "100", "102.5", "99", "102"),
        _candle(minute + timedelta(minutes=2), "102", "103", "97.5", "98"),
    )

    outcome = _evaluate(conclusion, candles, evaluated_at=minute + timedelta(minutes=3))

    assert outcome is not None
    assert outcome.target_touched is None
    assert outcome.invalidation_touched is False
    assert outcome.error_type is None


def test_outcome_marks_completed_5m_direction_structure_invalidation() -> None:
    conclusion = _conclusion()
    minute = conclusion.generated_at.replace(second=0, microsecond=0)
    candles = (
        _candle(minute + timedelta(minutes=1), "100", "101", "97.5", "98"),
        _candle(minute + timedelta(minutes=2), "98", "102.5", "97", "102"),
    )

    completed_5m = Candle(
        symbol="ACUUSDT",
        timeframe="5m",
        open_time=minute,
        close_time=minute + timedelta(minutes=5),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("97.5"),
        volume=Decimal("100"),
        turnover=Decimal("10000"),
        completed=True,
        source="BYBIT",
    )
    outcome = _evaluate(
        conclusion,
        candles,
        evaluated_at=minute + timedelta(minutes=5),
        invalidation_candles=(completed_5m,),
    )

    assert outcome is not None
    assert outcome.invalidation_touched is True
    assert outcome.details["invalidation_metric"] == "COMPLETED_5M_CLOSE"
    assert outcome.error_type == "DIRECTION_STRUCTURE_INVALIDATED"


def test_outcome_ignores_display_only_take_profit() -> None:
    conclusion = _conclusion()
    minute = conclusion.generated_at.replace(second=0, microsecond=0)
    candles = (
        _candle(minute + timedelta(minutes=1), "100", "102.5", "97.5", "100"),
    )

    outcome = _evaluate(conclusion, candles, evaluated_at=minute + timedelta(minutes=2))

    assert outcome is not None
    assert outcome.target_touched is None
    assert outcome.invalidation_touched is False
    assert outcome.error_type is None
