from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from bybit_signal.analysis.codex import (
    CodexAnalysisError,
    CodexAnalysisResult,
    CodexMonitoringReviewResult,
)
from bybit_signal.config import AppSettings, MonitoringConfig
from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    Direction,
    FreshnessDecision,
    MonitoringMetric,
    MonitoringReviewDecision,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ThresholdSeverity,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    AnalysisToolResult,
    CandidateAssessment,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    FormingHourOutlook,
    FreshnessAssessment,
    InvalidationCondition,
    ModelAnalysisResponse,
    ModelMonitoringReviewResponse,
    MonitoringDirective,
    MonitoringRuleReview,
    PriceLevel,
    SignalConclusion,
    SignalOutcome,
    ToolAssessment,
)
from bybit_signal.orchestration.cycle import SignalCycleService
from bybit_signal.selection.scanner import (
    CandidateFeatures,
    RankedCandidate,
    ScanResult,
)
from bybit_signal.storage.sqlite import SignalStore


def _bundle(symbol: str, price: str = "100") -> EvidenceBundle:
    now = datetime.now(UTC)
    one_minute_evidence_id = f"{symbol}.PA.1M"
    five_minute_evidence_id = f"{symbol}.PA.5M"
    price_value = Decimal(price)
    atr = max(price_value * Decimal("0.01"), Decimal("0.1"))
    return EvidenceBundle(
        symbol=symbol,
        generated_at=now,
        source_snapshot_sha256=("a" if symbol == "CYSUSDT" else "b") * 64,
        canonical_last=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=price_value,
            timestamp=now,
        ),
        canonical_mark=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.MARK,
            value=price_value + Decimal("0.1"),
            timestamp=now,
        ),
        evidence_items=(
            EvidenceItem(
                evidence_id=one_minute_evidence_id,
                category="price_action",
                source="TEST",
                observed_at=now,
                summary="completed 1m evidence",
                values={
                    "latest_close": float(price_value),
                    "atr_14": float(atr),
                    "latest_turnover": 1_000_000.0,
                },
            ),
            EvidenceItem(
                evidence_id=five_minute_evidence_id,
                category="price_action",
                source="TEST",
                observed_at=now,
                summary="completed 5m evidence",
                values={
                    "latest_close": float(price_value),
                    "atr_14": float(atr),
                },
            ),
        ),
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="test evidence ready",
                evidence_ids=(one_minute_evidence_id, five_minute_evidence_id),
            ),
        ),
    )


def _strong_conclusion(
    analysis_id: str,
    symbol: str = "CYSUSDT",
    *,
    rank: int = 1,
) -> SignalConclusion:
    now = datetime.now(UTC)
    evidence_id = f"{symbol}.PA.5M"
    assessment = CandidateAssessment(
        symbol=symbol,
        strength=SignalStrength.STRONG,
        selection_rank=rank,  # type: ignore[arg-type]
        confidence=SignalConfidence.MEDIUM,
        direction=Direction.LONG_BIAS,
        market_state="趋势修复",
        take_profit=PriceLevel(
            value=Decimal("102"),
            rationale="near structure target",
            evidence_ids=(evidence_id,),
        ),
        forming_1h=FormingHourOutlook(
            direction=Direction.LONG_BIAS,
            strength="NORMAL",
            window_start=now.replace(minute=0, second=0, microsecond=0),
            window_end=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
            rationale="forming hour structure",
        ),
        invalidation=InvalidationCondition(
            condition="completed 5m closes below structure",
            reference_price=Decimal("98"),
            evidence_ids=(evidence_id,),
        ),
        summary="strong long fixture",
        details="fixture details with completed evidence",
        evidence_ids=(evidence_id,),
        monitoring_directives=(
            MonitoringDirective(
                family_id=f"{symbol.lower()}.invalidation.5m",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                comparator=Comparator.LESS_THAN,
                threshold=Decimal("98"),
                hysteresis=Decimal("0.1"),
                valid_for_seconds=3600,
                reason="completed candle invalidates the long structure",
                evidence_ids=(evidence_id,),
                severity=ThresholdSeverity.CRITICAL,
                confirmation="completed_5m",
            ),
        ),
    )
    return SignalConclusion(
        analysis_id=analysis_id,
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=Decimal("100"),
            timestamp=now,
        ),
        assessment=assessment,
        tracking_status=TrackingStatus.NEW,
        comparison_with_previous="no previous signal",
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="test evidence ready",
                evidence_ids=(evidence_id,),
            ),
        ),
    )


def _cycle(
    conclusion: SignalConclusion,
    *,
    mode: CycleMode = CycleMode.SCHEDULED,
) -> AnalysisCycleResult:
    now = datetime.now(UTC)
    return AnalysisCycleResult(
        analysis_id=conclusion.analysis_id,
        mode=mode,
        started_at=now,
        completed_at=now,
        candidate_symbols=(conclusion.assessment.symbol,),
        conclusions=(conclusion,),
        strong_signal_count=int(conclusion.assessment.strength is SignalStrength.STRONG),
        selected_symbols=(conclusion.assessment.symbol,)
        if conclusion.assessment.selection_rank is not None
        else (),
        selected_signal_count=int(conclusion.assessment.selection_rank is not None),
        context_sha256="c" * 64,
    )


def _watch_conclusion(
    analysis_id: str,
    symbol: str = "CYSUSDT",
) -> SignalConclusion:
    strong = _strong_conclusion(analysis_id, symbol)
    assessment = strong.assessment.model_copy(
        update={
            "strength": SignalStrength.WATCH,
            "selection_rank": None,
            "confidence": None,
            "monitoring_directives": (),
        }
    )
    return strong.model_copy(
        update={
            "analysis_id": analysis_id,
            "assessment": assessment,
            "tracking_status": TrackingStatus.WEAKENED,
        }
    )


async def test_store_round_trip_and_previous_strong_symbols(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "state" / "signals.db")
    await store.initialize()
    conclusion = _strong_conclusion("analysis_01")
    cycle = _cycle(conclusion)
    bundle = _bundle("CYSUSDT")

    await store.save_cycle(cycle, (bundle,))

    assert await store.latest_cycle() == cycle
    assert await store.conclusion("analysis_01", "CYSUSDT") == conclusion
    assert await store.previous_strong_symbols() == ("CYSUSDT",)
    assert await store.previous_scheduled_strong_conclusions() == (conclusion,)
    assert await store.latest_bundle("CYSUSDT") == bundle
    health = await store.health_summary()
    assert health["strong_signal_count"] == 1
    assert health["selected_signal_count"] == 1
    assert health["model_status"] == "SUCCESS"


async def test_store_persists_freshness_tools_thresholds_delivery_and_outcomes(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "state" / "audit.db")
    await store.initialize()
    now = datetime.now(UTC)
    tool = AnalysisToolResult(
        request_id="fresh_01",
        tool="latest_market",
        symbol="CYSUSDT",
        status=ToolStatus.AVAILABLE,
        requested_at=now,
        completed_at=now,
        latency_ms=3,
    )
    freshness = FreshnessAssessment(
        symbol="CYSUSDT",
        decision=FreshnessDecision.PASS,
        checked_at=now,
        original_price=Decimal("100"),
        latest_price=Decimal("100.1"),
        drift_percent=Decimal("0.1"),
        drift_atr_1m=Decimal("0.2"),
        spread_bps=Decimal("3"),
        depth_retention=Decimal("0.9"),
        book_available=True,
        reasons=("fresh enough",),
    )
    outcome = SignalOutcome(
        analysis_id="analysis_01",
        symbol="CYSUSDT",
        evaluated_at=now,
        signal_time=now - timedelta(minutes=30),
        direction=Direction.LONG_BIAS,
        reference_price=Decimal("100"),
        latest_price=Decimal("101"),
        target_touched=True,
        invalidation_touched=False,
        mfe_percent=Decimal("1.5"),
        mae_percent=Decimal("-0.2"),
    )

    await store.save_tool_results("analysis_01", (tool,))
    await store.save_freshness_checks("analysis_01", (freshness,))
    first_insert = await store.record_threshold_event(
        event_id="event_01",
        analysis_id="analysis_01",
        symbol="CYSUSDT",
        family_id="cys.structure.5m",
        observed_at=now.isoformat(),
        critical=False,
        payload={"observed_value": "101"},
    )
    duplicate_insert = await store.record_threshold_event(
        event_id="event_01",
        analysis_id="analysis_01",
        symbol="CYSUSDT",
        family_id="cys.structure.5m",
        observed_at=now.isoformat(),
        critical=False,
        payload={"observed_value": "101"},
    )
    sibling_insert = await store.record_threshold_event(
        event_id="event_02",
        analysis_id="analysis_01",
        symbol="CYSUSDT",
        family_id="cys.target.5m",
        observed_at=(now + timedelta(seconds=1)).isoformat(),
        critical=False,
        payload={"observed_value": "102"},
    )
    await store.mark_event_delivered("event_01", 123, "TRIGGER", now.isoformat())
    await store.save_outcomes((outcome,))

    assert await store.tool_results("analysis_01") == (tool,)
    assert await store.freshness_checks("analysis_01") == (freshness,)
    assert first_insert is True and duplicate_insert is False and sibling_insert is False
    assert await store.threshold_events(symbol="CYSUSDT") == ({"observed_value": "101"},)
    assert await store.triggered_directive_keys(("analysis_01",)) == frozenset(
        {("analysis_01", "CYSUSDT", "cys.structure.5m")}
    )
    assert await store.triggered_directive_keys(("another_analysis",)) == frozenset()
    assert await store.triggered_analysis_symbol_keys(("analysis_01",)) == frozenset(
        {("analysis_01", "CYSUSDT")}
    )
    assert await store.triggered_analysis_symbol_keys(("another_analysis",)) == frozenset()
    assert await store.event_delivery_exists("event_01", 123, "TRIGGER") is True
    assert await store.signal_outcomes(symbol="CYSUSDT") == (outcome,)


async def test_store_persists_and_clears_runtime_state(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "state" / "runtime.db")
    await store.initialize()

    assert await store.runtime_state("monitoring.pause") is None


async def test_monitoring_commit_rejects_late_scheduled_and_emergency_versions(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "state" / "cas.db")
    await store.initialize()
    first_conclusion = _strong_conclusion("cycle_scheduled_01")
    first = _cycle(first_conclusion)
    await store.save_cycle(first, (_bundle("CYSUSDT"),))

    assert await store.commit_monitoring_version(
        first,
        authoritative_scheduled_analysis_id=first.analysis_id,
    )

    second_conclusion = _strong_conclusion("cycle_scheduled_02")
    second = _cycle(second_conclusion)
    await store.save_cycle(second, (_bundle("CYSUSDT"),))

    assert not await store.commit_monitoring_version(
        first,
        authoritative_scheduled_analysis_id=first.analysis_id,
    )

    urgent_one_conclusion = _watch_conclusion("urgent_review_01").model_copy(
        update={
            "assessment": first_conclusion.assessment.model_copy(
                update={"selection_rank": None, "confidence": None}
            )
        }
    )
    urgent_one = _cycle(urgent_one_conclusion, mode=CycleMode.EMERGENCY)
    urgent_two_conclusion = urgent_one_conclusion.model_copy(
        update={"analysis_id": "urgent_review_02"}
    )
    urgent_two = _cycle(urgent_two_conclusion, mode=CycleMode.EMERGENCY)
    await store.save_cycle(urgent_one, (_bundle("CYSUSDT"),))
    await store.save_cycle(urgent_two, (_bundle("CYSUSDT"),))

    assert not await store.commit_monitoring_version(
        urgent_one,
        authoritative_scheduled_analysis_id=second.analysis_id,
    )
    assert await store.commit_monitoring_version(
        urgent_two,
        authoritative_scheduled_analysis_id=second.analysis_id,
    )
    await store.set_runtime_state("monitoring.pause", "2026-08-18T15:25:00+00:00")
    assert (
        await store.runtime_state("monitoring.pause")
        == "2026-08-18T15:25:00+00:00"
    )
    await store.delete_runtime_state("monitoring.pause")
    assert await store.runtime_state("monitoring.pause") is None


async def test_reversed_emergency_review_replaces_scheduled_monitoring_thresholds(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    scheduled = _strong_conclusion("cycle_analysis_01")
    await store.save_cycle(_cycle(scheduled), (_bundle("CYSUSDT"),))
    emergency_base = _watch_conclusion("urgent_analysis_02")
    replacement_directive = MonitoringDirective(
        family_id="cysusdt.reversal.confirmation.1m",
        metric=MonitoringMetric.COMPLETED_1M_CLOSE,
        comparator=Comparator.LESS_THAN,
        threshold=Decimal("99"),
        hysteresis=Decimal("0.05"),
        valid_for_seconds=3600,
        reason="fresh emergency review threshold",
        evidence_ids=("CYSUSDT.PA.5M",),
    )
    emergency = emergency_base.model_copy(
        update={
            "assessment": emergency_base.assessment.model_copy(
                update={
                    "direction": Direction.SHORT_BIAS,
                    "monitoring_directives": (replacement_directive,),
                }
            ),
            "tracking_status": TrackingStatus.REVERSED,
        }
    )
    await store.save_cycle(
        _cycle(emergency, mode=CycleMode.EMERGENCY),
        (_bundle("CYSUSDT"),),
    )

    assert await store.active_monitoring_conclusions() == (emergency,)
    active = (await store.active_monitoring_conclusions())[0]
    assert active.assessment.monitoring_directives == (replacement_directive,)
    assert (
        active.assessment.monitoring_directives
        != scheduled.assessment.monitoring_directives
    )


class FakeScanner:
    async def scan(self, *, limit: int = 5) -> ScanResult:
        assert limit == 5
        now = datetime.now(UTC)
        candidates = tuple(
            RankedCandidate(
                rank=rank,
                symbol=symbol,
                score=100 - rank,
                last_price=Decimal("20"),
                observed_at=now,
                features=CandidateFeatures(
                    median_range_percent=1,
                    realized_volatility_percent=1,
                    maximum_absolute_return_percent=2,
                    direction_efficiency=0.5,
                    recent_turnover_ratio=2,
                    recent_30m_turnover_usdt=100_000,
                    spread_bps=3,
                    turnover_24h_usdt=1_000_000,
                    completed_candles=60,
                    missing_intervals=0,
                ),
                reasons=("volatile",),
            )
            for rank, symbol in enumerate(
                ("GPSUSDT", "TUTUSDT", "ACEUSDT", "EDENUSDT", "REDUSDT"),
                start=1,
            )
        )
        return ScanResult(
            generated_at=now,
            universe_size=100,
            ticker_eligible_size=50,
            kline_analyzed_size=50,
            candidates=candidates,
            failures={},
        )


class FakeMarketCollector:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    async def collect_many(self, symbols: Any) -> tuple[dict[str, Any], dict[str, str]]:
        self.symbols.extend(symbols)
        return (
            {symbol: type("Snapshot", (), {"symbol": symbol})() for symbol in symbols},
            {},
        )


class FakeEvidenceBuilder:
    def build(self, snapshot: Any) -> EvidenceBundle:
        return _bundle(snapshot.symbol, "100" if snapshot.symbol == "CYSUSDT" else "20")


def _selected_assessment(
    symbol: str,
    rank: int,
    *,
    selected: bool = True,
) -> CandidateAssessment:
    evidence_id = f"{symbol}.PA.5M"
    if not selected:
        return CandidateAssessment(
            symbol=symbol,
            strength=SignalStrength.WATCH,
            market_state="relative opportunity is weaker",
            summary="not selected in this scheduled cycle",
            details="another two symbols have clearer near-term structures",
            evidence_ids=(evidence_id,),
        )
    observed = datetime.now(UTC).replace(second=0, microsecond=0)
    forming_15m = observed.replace(minute=observed.minute - observed.minute % 15)
    forming_30m = observed.replace(minute=observed.minute - observed.minute % 30)
    forming_1h = observed.replace(minute=0)

    def outlook(start: datetime, duration: timedelta) -> FormingHourOutlook:
        return FormingHourOutlook(
            direction=Direction.LONG_BIAS,
            strength="NORMAL",
            window_start=start,
            window_end=start + duration,
            rationale="completed multi-timeframe structure",
        )

    return CandidateAssessment(
        symbol=symbol,
        strength=SignalStrength.WATCH,
        selection_rank=rank,  # type: ignore[arg-type]
        confidence=SignalConfidence.MEDIUM,
        direction=Direction.LONG_BIAS,
        market_state="near-term trend continuation",
        take_profit=PriceLevel(
            value=Decimal("21"),
            rationale="nearby completed structure",
            evidence_ids=(evidence_id,),
        ),
        forming_15m=outlook(forming_15m, timedelta(minutes=15)),
        forming_30m=outlook(forming_30m, timedelta(minutes=30)),
        forming_1h=outlook(forming_1h, timedelta(hours=1)),
        next_15m=outlook(forming_15m + timedelta(minutes=15), timedelta(minutes=15)),
        invalidation=InvalidationCondition(
            condition="completed 5m closes below the structure",
            reference_price=Decimal("19"),
            evidence_ids=(evidence_id,),
        ),
        summary="relative-best long fixture",
        details="selected from the complete scheduled candidate batch",
        evidence_ids=(evidence_id,),
        monitoring_directives=(
            MonitoringDirective(
                family_id=f"{symbol.lower()}.invalidation.5m",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                comparator=Comparator.LESS_THAN,
                threshold=Decimal("19.5"),
                hysteresis=Decimal("0.1"),
                valid_for_seconds=3600,
                reason="completed candle invalidates the selected structure",
                evidence_ids=(evidence_id,),
                severity=ThresholdSeverity.CRITICAL,
                confirmation="completed_5m",
            ),
        ),
    )


class FakeAnalyzer:
    def __init__(self) -> None:
        self.review_calls: list[tuple[str, ...]] = []

    async def analyze(
        self,
        *,
        analysis_id: str,
        bundles: Any,
        tracked_symbols: Any = (),
        trigger_reasons: Any = (),
        mode: CycleMode = CycleMode.SCHEDULED,
    ) -> CodexAnalysisResult:
        if mode is CycleMode.EMERGENCY:
            assert tuple(tracked_symbols) == ("CYSUSDT",)
            bundle = next(iter(bundles))
            assessment = _selected_assessment(bundle.symbol, rank=1, selected=False)
            return CodexAnalysisResult(
                response=ModelAnalysisResponse(
                    mode=CycleMode.EMERGENCY,
                    analysis_id=analysis_id,
                    cycle_summary="dropped symbol reviewed independently",
                    assessments=(assessment,),
                ),
                context_sha256="e" * 64,
                latency_ms=5,
                attempts=1,
                usage={},
            )
        assert tuple(tracked_symbols) == ()
        assert mode is CycleMode.SCHEDULED
        assessments = tuple(
            _selected_assessment(
                bundle.symbol,
                rank=index,
                selected=index <= 2,
            )
            for index, bundle in enumerate(bundles, start=1)
        )
        return CodexAnalysisResult(
            response=ModelAnalysisResponse(
                mode=CycleMode.SCHEDULED,
                analysis_id=analysis_id,
                cycle_summary="all symbols assessed",
                assessments=assessments,
            ),
            context_sha256="d" * 64,
            latency_ms=10,
            attempts=1,
            usage={},
        )

    async def review_monitoring(
        self,
        *,
        analysis_id: str,
        assessments: Any,
        bundles: Any,
        repair_reasons: Any = None,
        maximum_repair_attempts: Any = None,
    ) -> CodexMonitoringReviewResult:
        del repair_reasons, maximum_repair_attempts
        self.review_calls.append(tuple(assessment.symbol for assessment in assessments))
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}

        def reviewed_directives(assessment: CandidateAssessment) -> tuple[MonitoringDirective, ...]:
            bundle = bundle_by_symbol[assessment.symbol]
            completed_5m = next(
                item
                for item in bundle.evidence_items
                if item.evidence_id.endswith(".PA.5M")
            )
            baseline = Decimal(str(completed_5m.values["latest_close"]))
            return tuple(
                directive.model_copy(update={"current_value": baseline})
                for directive in assessment.monitoring_directives
            )

        reviews = tuple(
            MonitoringRuleReview(
                symbol=assessment.symbol,
                decision=MonitoringReviewDecision.ACCEPTED,
                rationale="candidate completed-candle rule remains a counter-direction threat",
                directives=reviewed_directives(assessment),
            )
            for assessment in assessments
            if assessment.symbol in bundle_by_symbol
        )
        return CodexMonitoringReviewResult(
            response=ModelMonitoringReviewResponse(
                analysis_id=analysis_id,
                reviews=reviews,
            ),
            context_sha256="f" * 64,
            latency_ms=5,
            usage={},
        )


async def test_cycle_selects_exactly_two_and_marks_dropped_previous_signal_exited(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    prior = _strong_conclusion("cycle_analysis_00")
    await store.save_cycle(_cycle(prior), (_bundle("CYSUSDT"),))
    emergency = _watch_conclusion("urgent_analysis_01")
    await store.save_cycle(
        _cycle(emergency, mode=CycleMode.EMERGENCY),
        (_bundle("CYSUSDT"),),
    )
    market_collector = FakeMarketCollector()
    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=market_collector,  # type: ignore[arg-type]
        evidence_builder=FakeEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        store=store,
    )

    result = await service.run_cycle()

    assert result.candidate_symbols == (
        "GPSUSDT",
        "TUTUSDT",
        "ACEUSDT",
        "EDENUSDT",
        "REDUSDT",
    )
    assert result.tracked_symbols == ("CYSUSDT",)
    assert market_collector.symbols == [
        "GPSUSDT",
        "TUTUSDT",
        "ACEUSDT",
        "EDENUSDT",
        "REDUSDT",
        "CYSUSDT",
    ]
    assert result.selected_signal_count == 2
    assert result.selected_symbols == ("GPSUSDT", "TUTUSDT")
    cys = next(
        conclusion for conclusion in result.conclusions if conclusion.assessment.symbol == "CYSUSDT"
    )
    assert cys.assessment.strength is SignalStrength.WATCH
    assert cys.assessment.monitoring_directives == ()
    assert cys.tracking_status is TrackingStatus.EXITED
    assert "已停止实时监测" in cys.comparison_with_previous


async def test_cycle_mechanically_rebases_unmet_activation_threshold(
    tmp_path: Path,
) -> None:
    class MovingEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            bundle = super().build(snapshot)
            if symbol != "GPSUSDT" or self.calls[symbol] < 3:
                return bundle
            items = tuple(
                item.model_copy(update={"values": {**item.values, "latest_close": 19.8}})
                if item.evidence_id.endswith(".PA.5M")
                else item
                for item in bundle.evidence_items
            )
            return bundle.model_copy(update={"evidence_items": items})

    store = SignalStore(tmp_path / "signals.db")
    market_collector = FakeMarketCollector()
    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=market_collector,  # type: ignore[arg-type]
        evidence_builder=MovingEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        store=store,
    )

    result = await service.run_cycle()

    assert result.status.value == "SUCCESS"
    assert result.selected_symbols == ("GPSUSDT", "TUTUSDT")
    assert result.selected_signal_count == 2
    monitoring = await service.review_and_activate_monitoring(result)

    assert monitoring.status == "READY"
    activation = cast(dict[str, Any], monitoring.diagnostics["activation"])
    assert activation["status"] == "READY"
    assert "initial_activation_errors" not in activation
    assert "repair" not in activation
    gps = next(
        conclusion
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.symbol == "GPSUSDT"
    )
    assert gps.assessment.monitoring_directives[0].current_value == Decimal("19.8")


async def test_no_qualified_monitoring_rules_is_a_successful_optional_stage(
    tmp_path: Path,
) -> None:
    class NoRuleAnalyzer(FakeAnalyzer):
        async def review_monitoring(self, **kwargs: Any) -> CodexMonitoringReviewResult:
            assessments = kwargs["assessments"]
            self.review_calls.append(
                tuple(assessment.symbol for assessment in assessments)
            )
            reviews = tuple(
                MonitoringRuleReview(
                    symbol=assessment.symbol,
                    decision=MonitoringReviewDecision.REJECTED,
                    rationale="当前没有脱离普通噪声且具备提前量的反向结构阈值",
                    directives=(),
                )
                for assessment in assessments
            )
            return CodexMonitoringReviewResult(
                response=ModelMonitoringReviewResponse(
                    analysis_id=kwargs["analysis_id"],
                    reviews=reviews,
                ),
                context_sha256="e" * 64,
                latency_ms=5,
                usage={},
            )

    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=FakeEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=NoRuleAnalyzer(),  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert monitoring.status == "READY"
    assert monitoring.committed is True
    assert monitoring.errors == {}
    activation = cast(dict[str, Any], monitoring.diagnostics["activation"])
    assert activation["status"] == "NOT_REQUIRED"
    assert all(
        not conclusion.assessment.monitoring_directives
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.selection_rank is not None
    )


async def test_activation_repair_exhaustion_retries_only_failed_symbol(
    tmp_path: Path,
) -> None:
    class RepeatedlyMovingEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            bundle = super().build(snapshot)
            if symbol != "GPSUSDT" or self.calls[symbol] < 3:
                return bundle
            next_close = 19.4 if self.calls[symbol] % 2 else 19.3
            items = tuple(
                item.model_copy(
                    update={"values": {**item.values, "latest_close": next_close}}
                )
                if item.evidence_id.endswith(".PA.5M")
                else item
                for item in bundle.evidence_items
            )
            return bundle.model_copy(update={"evidence_items": items})

    analyzer = FakeAnalyzer()
    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=RepeatedlyMovingEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert monitoring.status == "PARTIAL"
    activation = cast(dict[str, Any], monitoring.diagnostics["activation"])
    assert activation["repair"]["status"] == "EXHAUSTED"
    assert activation["repair"]["symbols"]["GPSUSDT"]["attempts"] == 3
    assert "GPSUSDT" in activation["activation_errors"]
    selected = {
        conclusion.assessment.symbol: conclusion.assessment
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.selection_rank is not None
    }
    assert selected["GPSUSDT"].monitoring_directives == ()
    assert selected["TUTUSDT"].monitoring_directives
    assert analyzer.review_calls == [
        ("GPSUSDT", "TUTUSDT"),
        ("GPSUSDT",),
        ("GPSUSDT",),
        ("GPSUSDT",),
    ]


async def test_activation_repair_may_end_with_no_rule_without_failure(
    tmp_path: Path,
) -> None:
    class CrossedEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            bundle = super().build(snapshot)
            if symbol != "GPSUSDT" or self.calls[symbol] < 3:
                return bundle
            items = tuple(
                item.model_copy(
                    update={"values": {**item.values, "latest_close": 19.4}}
                )
                if item.evidence_id.endswith(".PA.5M")
                else item
                for item in bundle.evidence_items
            )
            return bundle.model_copy(update={"evidence_items": items})

    class DecliningRepairAnalyzer(FakeAnalyzer):
        async def review_monitoring(self, **kwargs: Any) -> CodexMonitoringReviewResult:
            assessments = kwargs["assessments"]
            if not kwargs.get("repair_reasons"):
                return await super().review_monitoring(**kwargs)
            self.review_calls.append(
                tuple(assessment.symbol for assessment in assessments)
            )
            reviews = tuple(
                MonitoringRuleReview(
                    symbol=assessment.symbol,
                    decision=MonitoringReviewDecision.REJECTED,
                    rationale="交付时旧条件已经越线, 当前没有新的高质量结构阈值",
                    directives=(),
                )
                for assessment in assessments
            )
            return CodexMonitoringReviewResult(
                response=ModelMonitoringReviewResponse(
                    analysis_id=kwargs["analysis_id"],
                    reviews=reviews,
                ),
                context_sha256="a" * 64,
                latency_ms=5,
                usage={},
            )

    analyzer = DecliningRepairAnalyzer()
    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=CrossedEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert monitoring.status == "READY"
    assert monitoring.errors == {}
    activation = cast(dict[str, Any], monitoring.diagnostics["activation"])
    assert activation["repair"]["status"] == "READY"
    assert (
        activation["repair"]["symbols"]["GPSUSDT"]["final_status"]
        == "NO_QUALIFIED_RULE"
    )
    selected = {
        conclusion.assessment.symbol: conclusion.assessment
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.selection_rank is not None
    }
    assert selected["GPSUSDT"].monitoring_directives == ()
    assert selected["TUTUSDT"].monitoring_directives
    assert analyzer.review_calls == [("GPSUSDT", "TUTUSDT"), ("GPSUSDT",)]


async def test_activation_repair_permanent_model_error_stops_immediately(
    tmp_path: Path,
) -> None:
    class MovingEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            bundle = super().build(snapshot)
            if symbol != "GPSUSDT" or self.calls[symbol] < 3:
                return bundle
            items = tuple(
                item.model_copy(
                        update={"values": {**item.values, "latest_close": 19.4}}
                )
                if item.evidence_id.endswith(".PA.5M")
                else item
                for item in bundle.evidence_items
            )
            return bundle.model_copy(update={"evidence_items": items})

    class PermanentFailureAnalyzer(FakeAnalyzer):
        async def review_monitoring(self, **kwargs: Any) -> CodexMonitoringReviewResult:
            assessments = kwargs["assessments"]
            if kwargs.get("repair_reasons"):
                self.review_calls.append(
                    tuple(assessment.symbol for assessment in assessments)
                )
                raise CodexAnalysisError(
                    "CODEX_CREDITS_EXHAUSTED", "workspace is out of credits"
                )
            return await super().review_monitoring(**kwargs)

    analyzer = PermanentFailureAnalyzer()
    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=MovingEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=analyzer,  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert monitoring.status == "PARTIAL"
    assert monitoring.errors["GPSUSDT"].startswith("CODEX_CREDITS_EXHAUSTED:")
    activation = cast(dict[str, Any], monitoring.diagnostics["activation"])
    assert activation["repair"]["symbols"]["GPSUSDT"]["attempts"] == 1
    assert analyzer.review_calls == [("GPSUSDT", "TUTUSDT"), ("GPSUSDT",)]


async def test_cycle_skips_monitoring_activation_when_monitoring_is_disabled(
    tmp_path: Path,
) -> None:
    market_collector = FakeMarketCollector()
    service = SignalCycleService(
        AppSettings(monitoring=MonitoringConfig(enabled=False)),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=market_collector,  # type: ignore[arg-type]
        evidence_builder=FakeEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    assert result.status.value == "SUCCESS"
    assert result.selected_symbols == ("GPSUSDT", "TUTUSDT")
    assert result.diagnostics["monitoring_activation"] == {
        "status": "DISABLED",
        "symbols": {},
    }
    assert market_collector.symbols == [
        "GPSUSDT",
        "TUTUSDT",
        "ACEUSDT",
        "EDENUSDT",
        "REDUSDT",
    ]


async def test_cycle_uses_final_activation_price_as_telegram_reference(
    tmp_path: Path,
) -> None:
    class LatestPriceEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            if self.calls[symbol] > 1 and symbol in {"GPSUSDT", "TUTUSDT"}:
                bundle = super().build(snapshot)
                value = Decimal("20.2" if symbol == "GPSUSDT" else "20.1")
                return bundle.model_copy(
                    update={
                        "canonical_last": bundle.canonical_last.model_copy(
                            update={"value": value}
                        )
                    }
                )
            return super().build(snapshot)

    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=LatestPriceEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert result.status.value == "SUCCESS"
    selected = {
        conclusion.assessment.symbol: conclusion
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.selection_rank is not None
    }
    assert selected["GPSUSDT"].canonical_price.value == Decimal("20.2")
    assert selected["TUTUSDT"].canonical_price.value == Decimal("20.1")


async def test_cycle_activation_ignores_approximate_take_profit_already_passed(
    tmp_path: Path,
) -> None:
    class TargetPassedEvidenceBuilder(FakeEvidenceBuilder):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def build(self, snapshot: Any) -> EvidenceBundle:
            symbol = snapshot.symbol
            self.calls[symbol] = self.calls.get(symbol, 0) + 1
            bundle = super().build(snapshot)
            if self.calls[symbol] == 1 or symbol not in {"GPSUSDT", "TUTUSDT"}:
                return bundle
            last = bundle.canonical_last.model_copy(update={"value": Decimal("22")})
            mark = bundle.canonical_mark.model_copy(update={"value": Decimal("22.1")})
            return bundle.model_copy(update={"canonical_last": last, "canonical_mark": mark})

    service = SignalCycleService(
        AppSettings(),
        scanner=FakeScanner(),  # type: ignore[arg-type]
        market_collector=FakeMarketCollector(),  # type: ignore[arg-type]
        evidence_builder=TargetPassedEvidenceBuilder(),  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        store=SignalStore(tmp_path / "signals.db"),
    )

    result = await service.run_cycle()
    monitoring = await service.review_and_activate_monitoring(result)

    assert result.status.value == "SUCCESS"
    assert result.selected_symbols == ("GPSUSDT", "TUTUSDT")
    assert all(
        conclusion.canonical_price.value == Decimal("22")
        for conclusion in monitoring.cycle.conclusions
        if conclusion.assessment.selection_rank is not None
    )


def test_non_invalidation_review_trigger_can_keep_direction_maintained() -> None:
    previous = _strong_conclusion("cycle_analysis_00")
    current = previous.assessment.model_copy(
        update={"selection_rank": None, "confidence": None}
    )
    bundle = _bundle("CYSUSDT", "100")
    last_below_invalidation = bundle.canonical_last.model_copy(update={"value": Decimal("97")})
    bundle = bundle.model_copy(update={"canonical_last": last_below_invalidation})

    status, comparison = SignalCycleService._compare_emergency(previous, current, bundle)

    assert status is TrackingStatus.MAINTAINED
    assert "尚未失效" in comparison

    crossed_items = tuple(
        item.model_copy(update={"values": {**item.values, "latest_close": 97.0}})
        if item.evidence_id.endswith(".PA.5M")
        else item
        for item in bundle.evidence_items
    )
    crossed = bundle.model_copy(update={"evidence_items": crossed_items})
    status, comparison = SignalCycleService._compare_emergency(previous, current, crossed)
    assert status is TrackingStatus.MAINTAINED
    assert "尚未失效" in comparison


def test_previous_direction_invalidation_does_not_depend_on_monitoring_rules() -> None:
    previous = _strong_conclusion("cycle_analysis_00")
    previous = previous.model_copy(
        update={
            "assessment": previous.assessment.model_copy(
                update={"monitoring_directives": ()}
            )
        }
    )

    assert SignalCycleService._prior_invalidated(
        previous,
        _bundle("CYSUSDT", "97"),
    )
