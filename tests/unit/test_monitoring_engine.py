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
    MonitoringCondition,
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
                    metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                    comparator=Comparator.GREATER_THAN,
                    threshold=Decimal("100"),
                    hysteresis=Decimal("2"),
                    valid_for_seconds=valid_for_seconds,
                    reason="short structure invalidation",
                    evidence_ids=(evidence_id,),
                    current_value=Decimal("99"),
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


def test_threshold_crossing_is_one_shot_and_does_not_fire_on_startup() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("99"),
        observed_at=now,
    )
    first = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=1),
    )
    assert len(first) == 1
    assert first[0].critical is True
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("99"),
        observed_at=now + timedelta(seconds=2),
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("98"),
        observed_at=now + timedelta(seconds=3),
    )
    second = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=4),
    )
    assert second == ()


def test_threshold_already_breached_at_startup_is_baselined_not_fired() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now,
    )


def test_persisted_trigger_suspends_entire_analysis_symbol_after_restart() -> None:
    now = datetime.now(UTC)
    cycle = _cycle(now)
    original = cycle.conclusions[0]
    sibling = original.assessment.monitoring_directives[0].model_copy(
        update={
            "family_id": "cys.target.last",
            "metric": MonitoringMetric.LAST_PRICE,
            "threshold": Decimal("102"),
        }
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={
                    "monitoring_directives": (
                        *original.assessment.monitoring_directives,
                        sibling,
                    )
                }
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions(
        (conclusion,),
        now=now,
        suspended_keys=frozenset({(conclusion.analysis_id, "CYSUSDT")}),
    )

    assert engine.directives() == ()
    assert len(engine.directives(include_suspended=True)) == 1
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=1),
    )


def test_first_crossing_suspends_all_sibling_directives_immediately() -> None:
    now = datetime.now(UTC)
    original = _cycle(now).conclusions[0]
    sibling = original.assessment.monitoring_directives[0].model_copy(
        update={
            "family_id": "cys.structure.warning.15m",
            "metric": MonitoringMetric.COMPLETED_15M_CLOSE,
            "threshold": Decimal("102"),
        }
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={
                    "monitoring_directives": (
                        *original.assessment.monitoring_directives,
                        sibling,
                    )
                }
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=1),
    )

    assert len(events) == 1
    assert events[0].family_id == "cys.price.invalidation.short"
    assert engine.directives() == ()
    assert len(engine.directives(include_suspended=True)) == 2
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_15M_CLOSE,
        value=Decimal("103"),
        observed_at=now + timedelta(seconds=2),
    )


def test_first_completed_candle_after_directive_can_trigger_immediately() -> None:
    now = datetime.now(UTC)
    cycle = _cycle(now)
    original = cycle.conclusions[0]
    directive = original.assessment.monitoring_directives[0].model_copy(
        update={"metric": MonitoringMetric.COMPLETED_5M_CLOSE}
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(minutes=5),
    )

    assert len(events) == 1
    assert events[0].metric is MonitoringMetric.COMPLETED_5M_CLOSE
    assert events[0].observed_value == Decimal("101")


def test_rule_can_require_two_distinct_completed_candles() -> None:
    now = datetime.now(UTC)
    original = _cycle(now).conclusions[0]
    directive = original.assessment.monitoring_directives[0].model_copy(
        update={"required_consecutive_observations": 2}
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(minutes=5),
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101.1"),
        observed_at=now + timedelta(minutes=5, seconds=1),
    )
    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101.2"),
        observed_at=now + timedelta(minutes=10),
    )

    assert len(events) == 1
    assert events[0].observed_value == Decimal("101.2")


def test_composite_rule_waits_for_price_and_auxiliary_confirmation() -> None:
    now = datetime.now(UTC)
    original = _cycle(now).conclusions[0]
    directive = original.assessment.monitoring_directives[0].model_copy(
        update={
            "confirmations": (
                MonitoringCondition(
                    metric=MonitoringMetric.TRADE_DELTA_30S,
                    comparator=Comparator.LESS_THAN,
                    threshold=Decimal("-1000"),
                    current_value=Decimal("500"),
                    evidence_ids=("CYSUSDT.PA.5M",),
                    confirmation="rolling_30s_complete",
                ),
            )
        }
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=1),
    )
    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.TRADE_DELTA_30S,
        value=Decimal("-1500"),
        observed_at=now + timedelta(seconds=2),
    )

    assert len(events) == 1
    assert events[0].metric is MonitoringMetric.COMPLETED_5M_CLOSE
    assert events[0].observed_value == Decimal("101")
    assert [condition.metric for condition in events[0].matched_conditions] == [
        MonitoringMetric.COMPLETED_5M_CLOSE,
        MonitoringMetric.TRADE_DELTA_30S,
    ]
    assert [condition.observed_value for condition in events[0].matched_conditions] == [
        Decimal("101"),
        Decimal("-1500"),
    ]


def test_auxiliary_confirmation_alone_never_triggers_composite_rule() -> None:
    now = datetime.now(UTC)
    original = _cycle(now).conclusions[0]
    directive = original.assessment.monitoring_directives[0].model_copy(
        update={
            "confirmations": (
                MonitoringCondition(
                    metric=MonitoringMetric.OPEN_INTEREST,
                    comparator=Comparator.LESS_THAN,
                    threshold=Decimal("9000"),
                    current_value=Decimal("10000"),
                    evidence_ids=("CYSUSDT.PA.5M",),
                    confirmation="ticker_snapshot",
                ),
            )
        }
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.OPEN_INTEREST,
        value=Decimal("8000"),
        observed_at=now + timedelta(seconds=1),
    )
    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(minutes=5),
    )

    assert len(events) == 1


def test_consecutive_primary_and_price_confirmation_are_atomic() -> None:
    now = datetime.now(UTC)
    original = _cycle(now).conclusions[0]
    directive = original.assessment.monitoring_directives[0].model_copy(
        update={
            "metric": MonitoringMetric.COMPLETED_1M_CLOSE,
            "comparator": Comparator.LESS_THAN,
            "threshold": Decimal("0.001861"),
            "hysteresis": Decimal("0.000004"),
            "current_value": Decimal("0.001884"),
            "required_consecutive_observations": 2,
            "confirmations": (
                MonitoringCondition(
                    metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                    comparator=Comparator.LESS_THAN,
                    threshold=Decimal("0.001861"),
                    current_value=Decimal("0.001884"),
                    evidence_ids=("CYSUSDT.PA.5M",),
                    confirmation="completed_5m_below_same_pivot",
                ),
            ),
        }
    )
    conclusion = original.model_copy(
        update={
            "assessment": original.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    engine = ThresholdEngine()
    engine.replace_conclusions((conclusion,), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_1M_CLOSE,
        value=Decimal("0.001856"),
        observed_at=now + timedelta(minutes=1),
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_1M_CLOSE,
        value=Decimal("0.001854"),
        observed_at=now + timedelta(minutes=2),
    )
    events = engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("0.001845"),
        observed_at=now + timedelta(minutes=5),
    )

    assert len(events) == 1
    assert [item.metric for item in events[0].matched_conditions] == [
        MonitoringMetric.COMPLETED_1M_CLOSE,
        MonitoringMetric.COMPLETED_5M_CLOSE,
    ]
    assert [item.observed_value for item in events[0].matched_conditions] == [
        Decimal("0.001854"),
        Decimal("0.001845"),
    ]


def test_expired_directive_is_removed() -> None:
    now = datetime.now(UTC)
    engine = ThresholdEngine()
    engine.replace_cycle(_cycle(now, valid_for_seconds=60), now=now)

    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("99"),
        observed_at=now,
    )
    assert not engine.observe(
        symbol="CYSUSDT",
        metric=MonitoringMetric.COMPLETED_5M_CLOSE,
        value=Decimal("101"),
        observed_at=now + timedelta(seconds=61),
    )
    assert engine.directives() == ()


def test_wake_limiter_reports_soft_hourly_budget_without_blocking_critical() -> None:
    now = datetime.now(UTC)
    limiter = WakeLimiter(MonitoringConfig(soft_model_wakes_per_hour=2))

    assert limiter.allow(now=now, critical=False)
    assert limiter.allow(now=now + timedelta(seconds=1), critical=False)
    assert not limiter.allow(now=now + timedelta(seconds=2), critical=False)
    assert limiter.allow(now=now + timedelta(seconds=3), critical=True)
    assert limiter.allow(now=now + timedelta(hours=1, seconds=3), critical=False)
