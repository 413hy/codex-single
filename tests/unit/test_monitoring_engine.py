from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import (
    Comparator,
    MonitoringMetric,
    PriceType,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandidateAssessment,
    CanonicalPrice,
    MonitoringDirective,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.monitoring.engine import ThresholdEngine, WakeLimiter


def _cycle(now: datetime, *, valid_for_seconds: int = 3600) -> AnalysisCycleResult:
    evidence_id = "CYSUSDT.PA.5M"
    conclusion = SignalConclusion(
        analysis_id="analysis_01",
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol="CYSUSDT",
            price_type=PriceType.LAST,
            value=Decimal("99"),
            timestamp=now,
        ),
        assessment=CandidateAssessment(
            symbol="CYSUSDT",
            strength=SignalStrength.NO_STRONG_SIGNAL,
            market_state="monitoring fixture",
            summary="monitor threshold fixture",
            details="monitor threshold fixture details",
            evidence_ids=(evidence_id,),
            monitoring_directives=(
                MonitoringDirective(
                    family_id="cys.price.invalidation.short",
                    metric=MonitoringMetric.LAST_PRICE,
                    comparator=Comparator.GREATER_THAN,
                    threshold=Decimal("100"),
                    hysteresis=Decimal("2"),
                    valid_for_seconds=valid_for_seconds,
                    reason="short structure invalidation",
                    evidence_ids=(evidence_id,),
                ),
            ),
        ),
        tracking_status=TrackingStatus.INDETERMINATE,
        comparison_with_previous="fixture comparison",
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    return AnalysisCycleResult(
        analysis_id="analysis_01",
        started_at=now,
        completed_at=now,
        candidate_symbols=("CYSUSDT",),
        conclusions=(conclusion,),
        strong_signal_count=0,
        context_sha256="a" * 64,
    )


def test_threshold_crossing_uses_hysteresis_and_does_not_fire_on_startup() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("99"),
        observed_at=now,
    )
    first = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=1),
    )
    assert len(first) == 1
    assert first[0].critical is True
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("99"),
        observed_at=now + timedelta(seconds=2),
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("98"),
        observed_at=now + timedelta(seconds=3),
    )
    second = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=4),
    )
    assert len(second) == 1


def test_threshold_already_breached_at_startup_is_baselined_not_fired() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("101"),
        observed_at=now,
    )


def test_expired_directive_is_removed() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now, valid_for_seconds=60), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("99"),
        observed_at=now,
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.LAST_PRICE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=61),
    )
    assert engine.directives() == ()


def test_wake_limiter_applies_symbol_cooldown_and_soft_hourly_cap() -> None:
    now = datetime.now(UTC)
    limiter = WakeLimiter(
        MonitoringConfig(symbol_cooldown_seconds=60, soft_model_wakes_per_hour=2)
    )

    assert limiter.allow("CYSUSDT", now=now, critical=False)
    assert not limiter.allow(
        "CYSUSDT", now=now + timedelta(seconds=30), critical=True
    )
    assert limiter.allow("GPSUSDT", now=now + timedelta(seconds=61), critical=False)
    assert not limiter.allow(
        "ALICEUSDT", now=now + timedelta(seconds=62), critical=False
    )
    assert limiter.allow("ALICEUSDT", now=now + timedelta(seconds=62), critical=True)
