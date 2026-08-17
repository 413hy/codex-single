from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.domain.enums import PriceType, ToolStatus
from bybit_signal.domain.models import Candle
from bybit_signal.evidence.builder import EvidenceBuilder
from bybit_signal.providers.bybit import (
    BybitBookLevel,
    BybitOpenInterest,
    BybitOrderBook,
    BybitPublicTrade,
    BybitTicker,
)
from bybit_signal.providers.cross_exchange import ReferenceTicker
from bybit_signal.providers.deep_market import NativeMarketSnapshot


def _candles(symbol: str, timeframe: str, duration: timedelta) -> tuple[Candle, ...]:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    result = []
    for index in range(240):
        center = Decimal("100") + Decimal(index) / Decimal("10")
        open_time = start + duration * index
        result.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=open_time + duration,
                open=center,
                high=center + Decimal("0.8"),
                low=center - Decimal("0.7"),
                close=center + (Decimal("0.3") if index % 2 else Decimal("-0.2")),
                volume=Decimal("1000"),
                turnover=Decimal("100000") + Decimal(index * 100),
                completed=True,
                source="BYBIT",
            )
        )
    return tuple(result)


def _snapshot(*, partial_trades: bool = False) -> NativeMarketSnapshot:
    symbol = "CYSUSDT"
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    ticker = BybitTicker(
        symbol=symbol,
        last_price=Decimal("123.90"),
        mark_price=Decimal("123.85"),
        index_price=Decimal("123.80"),
        bid_price=Decimal("123.89"),
        ask_price=Decimal("123.91"),
        high_24h=Decimal("130"),
        low_24h=Decimal("110"),
        turnover_24h=Decimal("5000000"),
        volume_24h=Decimal("40000"),
        price_change_24h=Decimal("0.05"),
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("2000000"),
        open_interest_value=Decimal("240000000"),
        next_funding_time=now + timedelta(hours=4),
        observed_at=now,
    )
    book = BybitOrderBook(
        symbol=symbol,
        observed_at=now,
        update_id=10,
        sequence=11,
        bids=tuple(
            BybitBookLevel(price=Decimal("123.89") - i / Decimal("100"), size=100 + i)
            for i in range(20)
        ),
        asks=tuple(
            BybitBookLevel(price=Decimal("123.91") + i / Decimal("100"), size=90 + i)
            for i in range(20)
        ),
    )
    count = 10 if partial_trades else 80
    spacing = 2 if partial_trades else 5
    trades = tuple(
        BybitPublicTrade(
            symbol=symbol,
            trade_id=f"trade-{index}",
            timestamp=now - timedelta(seconds=(count - 1 - index) * spacing),
            side="Buy" if index % 3 else "Sell",
            price=Decimal("123.90"),
            size=Decimal("2"),
        )
        for index in range(count)
    )
    oi = tuple(
        BybitOpenInterest(
            symbol=symbol,
            timestamp=now - timedelta(minutes=(47 - index) * 5),
            open_interest=Decimal("1900000") + Decimal(index * 2000),
        )
        for index in range(48)
    )
    return NativeMarketSnapshot(
        symbol=symbol,
        collection_started_at=now - timedelta(seconds=2),
        generated_at=now,
        ticker=ticker,
        candles={
            "5m": _candles(symbol, "5m", timedelta(minutes=5)),
            "15m": _candles(symbol, "15m", timedelta(minutes=15)),
            "1h": _candles(symbol, "1h", timedelta(hours=1)),
            "4h": _candles(symbol, "4h", timedelta(hours=4)),
        },
        orderbook=book,
        recent_trades=trades,
        open_interest=oi,
        reference_tickers=(
            ReferenceTicker(
                symbol=symbol,
                exchange="BINANCE",
                instrument=symbol,
                last_price=Decimal("123.88"),
                bid_price=Decimal("123.87"),
                ask_price=Decimal("123.89"),
                observed_at=now,
            ),
        ),
    )


def test_builder_preserves_prices_and_derives_completed_candle_features() -> None:
    bundle = EvidenceBuilder().build(_snapshot())

    assert bundle.canonical_last.price_type is PriceType.LAST
    assert bundle.canonical_last.value != bundle.canonical_mark.value
    price_action = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.PA.5M"
    )
    assert price_action.values["completed_candles"] == 240
    assert float(price_action.values["rolling_high_20"]) < 1_000
    assert price_action.values["ema_50"] is not None
    assert price_action.values["return_1_percent"] is not None
    assert float(price_action.values["recent_30m_turnover_usdt"]) > 0
    assert float(price_action.values["drawdown_from_rolling_high_atr"]) >= 0
    assert float(price_action.values["rebound_from_rolling_low_atr"]) >= 0
    core = next(
        tool for tool in bundle.tool_assessments if tool.tool == "BYBIT_NATIVE_MARKET_DATA"
    )
    assert core.status is ToolStatus.AVAILABLE
    reference = next(
        item
        for item in bundle.evidence_items
        if item.evidence_id == "CYSUSDT.REFERENCE.BINANCE"
    )
    assert reference.values["last_divergence_vs_bybit_bps"] is not None


def test_builder_fail_closes_incomplete_public_trade_window() -> None:
    bundle = EvidenceBuilder().build(_snapshot(partial_trades=True))

    trade_window = next(
        item
        for item in bundle.evidence_items
        if item.evidence_id == "CYSUSDT.MICRO.TRADES.5M"
    )
    assert trade_window.values["qualified"] is False
    assert trade_window.values["normalized_delta"] is None


def test_confirmed_pivots_use_only_right_side_completed_bars() -> None:
    bundle = EvidenceBuilder().build(_snapshot())
    price_action = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.PA.5M"
    )

    latest = datetime.fromisoformat(str(price_action.values["latest_completed_close_time"]))
    for key in ("pivot_high_confirmed_at", "pivot_low_confirmed_at"):
        confirmed = price_action.values[key]
        assert confirmed is None or datetime.fromisoformat(str(confirmed)) <= latest
    for key in ("pivot_high_age_bars", "pivot_low_age_bars"):
        age = price_action.values[key]
        assert age is None or int(age) >= 0
