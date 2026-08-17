from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bybit_signal.analysis.codex import CodexAnalysisResult
from bybit_signal.config import AppSettings
from bybit_signal.domain.enums import (
    Direction,
    PriceType,
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
) -> SignalConclusion:
    now = datetime.now(UTC)
    evidence_id = f"{symbol}.PA.5M"
    assessment = CandidateAssessment(
        symbol=symbol,
        strength=SignalStrength.STRONG,
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


def _cycle(conclusion: SignalConclusion) -> AnalysisCycleResult:
    now = datetime.now(UTC)
    return AnalysisCycleResult(
        analysis_id=conclusion.analysis_id,
        started_at=now,
        completed_at=now,
        candidate_symbols=(conclusion.assessment.symbol,),
        conclusions=(conclusion,),
        strong_signal_count=1,
        context_sha256="c" * 64,
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
    assert await store.latest_bundle("CYSUSDT") == bundle
    health = await store.health_summary()
    assert health["strong_signal_count"] == 1


class FakeScanner:
    async def scan(self, *, limit: int = 5) -> ScanResult:
        assert limit == 5
        now = datetime.now(UTC)
        candidate = RankedCandidate(
            rank=1,
            symbol="GPSUSDT",
            score=100,
            last_price=Decimal("20"),
            observed_at=now,
            features=CandidateFeatures(
                median_range_percent=1,
                realized_volatility_percent=1,
                maximum_absolute_return_percent=2,
                direction_efficiency=0.5,
                recent_turnover_ratio=2,
                spread_bps=3,
                turnover_24h_usdt=1_000_000,
                completed_candles=60,
                missing_intervals=0,
            ),
            reasons=("volatile",),
        )
        return ScanResult(
            generated_at=now,
            universe_size=100,
            ticker_eligible_size=50,
            kline_analyzed_size=50,
            candidates=(candidate,),
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


class FakeAnalyzer:
    async def analyze(
        self,
        *,
        analysis_id: str,
        bundles: Any,
        tracked_symbols: Any = (),
    ) -> CodexAnalysisResult:
        assert tuple(tracked_symbols) == ("CYSUSDT",)
        assessments = tuple(
            CandidateAssessment(
                symbol=bundle.symbol,
                strength=SignalStrength.NO_STRONG_SIGNAL,
                market_state="无强信号",
                summary="当前没有足够强的信号",
                details="本轮证据不足以形成强信号。",
                evidence_ids=(f"{bundle.symbol}.PA.5M",),
            )
            for bundle in bundles
        )
        return CodexAnalysisResult(
            response=ModelAnalysisResponse(
                analysis_id=analysis_id,
                cycle_summary="all symbols assessed",
                assessments=assessments,
            ),
            context_sha256="d" * 64,
            latency_ms=10,
            attempts=1,
            usage={},
        )


async def test_cycle_always_reanalyzes_and_marks_previous_strong_signal_weakened(
    tmp_path: Path,
) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    prior = _strong_conclusion("analysis_00")
    await store.save_cycle(_cycle(prior), (_bundle("CYSUSDT"),))
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

    assert result.candidate_symbols == ("GPSUSDT",)
    assert result.tracked_symbols == ("CYSUSDT",)
    assert market_collector.symbols == ["GPSUSDT", "CYSUSDT"]
    cys = next(
        conclusion
        for conclusion in result.conclusions
        if conclusion.assessment.symbol == "CYSUSDT"
    )
    assert cys.assessment.strength is SignalStrength.NO_STRONG_SIGNAL
    assert cys.tracking_status is TrackingStatus.WEAKENED
    assert "上一轮强信号" in cys.comparison_with_previous
