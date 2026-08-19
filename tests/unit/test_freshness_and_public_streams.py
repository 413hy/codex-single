from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.analysis.freshness import FreshnessGate
from bybit_signal.config import FreshnessConfig
from bybit_signal.domain.enums import (
    Comparator,
    Direction,
    FreshnessDecision,
    MonitoringMetric,
    PriceType,
    SignalStrength,
    ThresholdSeverity,
    ToolStatus,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    Candle,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    InvalidationCondition,
    MonitoringDirective,
    PriceLevel,
    ToolAssessment,
)
from bybit_signal.providers.bybit import (
    BybitBookLevel,
    BybitOrderBook,
    BybitTicker,
)
from bybit_signal.providers.public_streams import PublicStreamCache

NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


def _bundle() -> EvidenceBundle:
    price = CanonicalPrice(
        symbol="ACUUSDT",
        price_type=PriceType.LAST,
        value=Decimal("0.13128"),
        timestamp=NOW,
    )
    items = (
        EvidenceItem(
            evidence_id="ACUUSDT.PA.1M",
            category="price_action",
            source="TEST",
            observed_at=NOW,
            summary="completed one minute evidence",
            values={"atr_14": 0.001},
        ),
        EvidenceItem(
            evidence_id="ACUUSDT.PA.5M",
            category="price_action",
            source="TEST",
            observed_at=NOW,
            summary="completed five minute evidence",
            values={"latest_close": 0.13128, "atr_14": 0.002},
        ),
        EvidenceItem(
            evidence_id="ACUUSDT.BYBIT.TICKER",
            category="ticker",
            source="TEST",
            observed_at=NOW,
            summary="ticker evidence",
            values={"spread_bps": 4.0},
        ),
        EvidenceItem(
            evidence_id="ACUUSDT.MICRO.BOOK",
            category="orderbook",
            source="TEST",
            observed_at=NOW,
            summary="point in time book",
            values={
                "bid_notional_top20": 10_000.0,
                "ask_notional_top20": 10_000.0,
            },
        ),
    )
    return EvidenceBundle(
        symbol="ACUUSDT",
        generated_at=NOW,
        source_snapshot_sha256="a" * 64,
        canonical_last=price,
        canonical_mark=price.model_copy(update={"price_type": PriceType.MARK}),
        evidence_items=items,
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="freshness fixture evidence",
                evidence_ids=tuple(item.evidence_id for item in items),
            ),
        ),
    )


def _assessment() -> CandidateAssessment:
    evidence = ("ACUUSDT.PA.1M",)
    return CandidateAssessment(
        symbol="ACUUSDT",
        strength=SignalStrength.WATCH,
        direction=Direction.LONG_BIAS,
        market_state="test long setup",
        take_profit=PriceLevel(
            value=Decimal("0.134"),
            rationale="near completed structure",
            evidence_ids=evidence,
        ),
        invalidation=InvalidationCondition(
            condition="completed five minute structure fails",
            reference_price=Decimal("0.128"),
            evidence_ids=("ACUUSDT.PA.5M",),
        ),
        summary="test assessment",
        details="test assessment details",
        evidence_ids=evidence,
        monitoring_directives=(
            MonitoringDirective(
                family_id="acuusdt.invalidation.5m",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                comparator=Comparator.LESS_THAN,
                threshold=Decimal("0.128"),
                hysteresis=Decimal("0.0001"),
                valid_for_seconds=1800,
                reason="completed 5m close invalidates the long direction",
                evidence_ids=("ACUUSDT.PA.5M",),
                severity=ThresholdSeverity.CRITICAL,
                confirmation="completed_5m",
            ),
        ),
    )


def _ticker(price: str, *, spread_bps: str = "4") -> BybitTicker:
    value = Decimal(price)
    half = value * Decimal(spread_bps) / Decimal(20_000)
    return BybitTicker(
        symbol="ACUUSDT",
        last_price=value,
        mark_price=value,
        index_price=value,
        bid_price=value - half,
        ask_price=value + half,
        high_24h=value * Decimal("1.2"),
        low_24h=value * Decimal("0.8"),
        turnover_24h=Decimal("10000000"),
        volume_24h=Decimal("100000000"),
        price_change_24h=Decimal("0"),
        observed_at=NOW + timedelta(seconds=150),
    )


def _book(size: str = "100000") -> BybitOrderBook:
    return BybitOrderBook(
        symbol="ACUUSDT",
        observed_at=NOW + timedelta(seconds=150),
        update_id=1,
        sequence=1,
        bids=(BybitBookLevel(price=Decimal("0.13"), size=Decimal(size)),),
        asks=(BybitBookLevel(price=Decimal("0.131"), size=Decimal(size)),),
    )


class _FreshClient:
    def __init__(
        self,
        ticker: BybitTicker,
        book: BybitOrderBook | None,
        *,
        completed_close: str = "0.131",
    ) -> None:
        self._ticker = ticker
        self._book = book
        self._completed_close = Decimal(completed_close)

    async def tickers(self) -> dict[str, BybitTicker]:
        return {"ACUUSDT": self._ticker}

    async def orderbook(self, symbol: str, *, limit: int = 50) -> BybitOrderBook:
        del symbol, limit
        if self._book is None:
            raise RuntimeError("book unavailable")
        return self._book

    async def completed_candles(
        self, symbol: str, *, timeframe: str, limit: int
    ) -> tuple[object, ...]:
        del limit
        minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}[timeframe]
        return (
            Candle(
                symbol=symbol,
                timeframe=timeframe,  # type: ignore[arg-type]
                open_time=NOW,
                close_time=NOW + timedelta(minutes=minutes),
                open=self._completed_close,
                high=self._completed_close,
                low=self._completed_close,
                close=self._completed_close,
                volume=Decimal("1"),
                turnover=Decimal("1"),
                completed=True,
                source="BYBIT",
            ),
        )


async def test_freshness_ignores_last_price_spike_without_completed_close_invalidation() -> None:
    bundle = _bundle()
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.126"),
            _book(),
            completed_close="0.129",
        ),
        FreshnessConfig(),
    )

    checks, returned = await gate.evaluate((_assessment(),), {"ACUUSDT": bundle})

    assert checks[0].decision is FreshnessDecision.PASS
    assert checks[0].latest_price == Decimal("0.126")
    assert returned["ACUUSDT"] == bundle
    assert returned["ACUUSDT"].canonical_last.value == Decimal("0.13128")


async def test_freshness_ignores_approximate_take_profit_reached_before_delivery() -> None:
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.140"),
            _book(),
            completed_close="0.139",
        ),
        FreshnessConfig(),
    )

    checks, _ = await gate.evaluate((_assessment(),), {"ACUUSDT": _bundle()})

    assert checks[0].decision is FreshnessDecision.PASS
    take_profit = _assessment().take_profit
    assert take_profit is not None
    assert checks[0].latest_price > take_profit.value


async def test_freshness_repairs_after_completed_5m_structure_invalidation() -> None:
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.127"),
            _book(),
            completed_close="0.127",
        ),
        FreshnessConfig(),
    )

    checks, _ = await gate.evaluate((_assessment(),), {"ACUUSDT": _bundle()})

    assert checks[0].decision is FreshnessDecision.REPAIR
    assert "已完成5m收盘" in checks[0].reasons[0]


async def test_freshness_supports_completed_30m_direction_invalidation() -> None:
    assessment = _assessment()
    invalidation = assessment.invalidation
    assert invalidation is not None
    assessment = assessment.model_copy(
        update={
            "invalidation": invalidation.model_copy(
                update={
                    "condition": "completed 30m close breaks the structure",
                    "evidence_ids": ("ACUUSDT.PA.30M",),
                }
            )
        }
    )
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.127"),
            _book(),
            completed_close="0.127",
        ),
        FreshnessConfig(),
    )

    checks, _ = await gate.evaluate((assessment,), {"ACUUSDT": _bundle()})

    assert checks[0].decision is FreshnessDecision.REPAIR
    assert "已完成30m收盘" in checks[0].reasons[0]


async def test_freshness_uses_model_invalidation_without_monitoring_candidates() -> None:
    assessment = _assessment().model_copy(update={"monitoring_directives": ()})
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.129"),
            _book(),
            completed_close="0.129",
        ),
        FreshnessConfig(),
    )

    checks, _ = await gate.evaluate((assessment,), {"ACUUSDT": _bundle()})

    assert checks[0].decision is FreshnessDecision.PASS
    assert "已完成5m收盘" in checks[0].reasons[0]


async def test_freshness_does_not_gate_direction_on_spread_or_orderbook() -> None:
    gate = FreshnessGate(
        _FreshClient(  # type: ignore[arg-type]
            _ticker("0.131", spread_bps="100"),
            None,
            completed_close="0.130",
        ),
        FreshnessConfig(),
    )

    checks, _ = await gate.evaluate((_assessment(),), {"ACUUSDT": _bundle()})

    assert checks[0].decision is FreshnessDecision.PASS
    assert checks[0].spread_bps == Decimal("100")
    assert checks[0].book_available is False


def test_liquidation_zero_is_only_emitted_after_complete_stream_coverage() -> None:
    cache = PublicStreamCache()
    cache.mark_subscribed("ACUUSDT", at=NOW)

    warming = cache.liquidation_window("ACUUSDT", seconds=300, now=NOW + timedelta(seconds=120))
    cache.heartbeat("ACUUSDT", at=NOW + timedelta(seconds=360))
    covered = cache.liquidation_window("ACUUSDT", seconds=300, now=NOW + timedelta(seconds=360))

    assert warming.status is ToolStatus.WARMING_UP
    assert warming.event_count is None
    assert covered.status is ToolStatus.AVAILABLE
    assert covered.coverage_complete is True
    assert covered.event_count == 0
    assert covered.long_notional == Decimal(0)
