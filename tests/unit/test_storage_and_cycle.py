from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bybit_signal.analysis.codex import CodexAnalysisResult
from bybit_signal.config import AppSettings
from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    Direction,
    MonitoringMetric,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandidateAssessment,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    FormingHourOutlook,
    InvalidationCondition,
    ModelAnalysisResponse,
    MonitoringDirective,
    PriceLevel,
    SignalConclusion,
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
    evidence_id = f"{symbol}.PA.5M"
    return EvidenceBundle(
        symbol=symbol,
        generated_at=now,
        source_snapshot_sha256=("a" if symbol == "CYSUSDT" else "b") * 64,
        canonical_last=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=Decimal(price),
            timestamp=now,
        ),
        canonical_mark=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.MARK,
            value=Decimal(price) + Decimal("0.1"),
            timestamp=now,
        ),
        evidence_items=(
            EvidenceItem(
                evidence_id=evidence_id,
                category="price_action",
                source="TEST",
                observed_at=now,
                summary="completed 5m evidence",
            ),
        ),
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="test evidence ready",
                evidence_ids=(evidence_id,),
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
            window_end=now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1),
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


async def test_reversed_emergency_review_stops_selected_signal_monitoring(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    scheduled = _strong_conclusion("cycle_analysis_01")
    await store.save_cycle(_cycle(scheduled), (_bundle("CYSUSDT"),))
    emergency_base = _watch_conclusion("urgent_analysis_02")
    emergency = emergency_base.model_copy(
        update={
            "assessment": emergency_base.assessment.model_copy(
                update={
                    "direction": Direction.SHORT_BIAS,
                    "monitoring_directives": scheduled.assessment.monitoring_directives,
                }
            ),
            "tracking_status": TrackingStatus.REVERSED,
        }
    )
    await store.save_cycle(
        _cycle(emergency, mode=CycleMode.EMERGENCY),
        (_bundle("CYSUSDT"),),
    )

    assert await store.active_monitoring_conclusions() == ()


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
            for rank, symbol in enumerate(("GPSUSDT", "TUTUSDT"), start=1)
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
            {
                symbol: type("Snapshot", (), {"symbol": symbol})()
                for symbol in symbols
            },
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
                threshold=Decimal("19"),
                hysteresis=Decimal("0.1"),
                valid_for_seconds=3600,
                reason="completed candle invalidates the selected structure",
                evidence_ids=(evidence_id,),
            ),
        ),
    )


class FakeAnalyzer:
    async def analyze(
        self,
        *,
        analysis_id: str,
        bundles: Any,
        tracked_symbols: Any = (),
        mode: CycleMode = CycleMode.SCHEDULED,
    ) -> CodexAnalysisResult:
        assert tuple(tracked_symbols) == ("CYSUSDT",)
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

    assert result.candidate_symbols == ("GPSUSDT", "TUTUSDT")
    assert result.tracked_symbols == ("CYSUSDT",)
    assert market_collector.symbols == ["GPSUSDT", "TUTUSDT", "CYSUSDT"]
    assert result.selected_signal_count == 2
    assert result.selected_symbols == ("GPSUSDT", "TUTUSDT")
    cys = next(
        conclusion
        for conclusion in result.conclusions
        if conclusion.assessment.symbol == "CYSUSDT"
    )
    assert cys.assessment.strength is SignalStrength.WATCH
    assert cys.assessment.monitoring_directives == ()
    assert cys.tracking_status is TrackingStatus.EXITED
    assert "已停止实时监测" in cys.comparison_with_previous
