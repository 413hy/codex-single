from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from bybit_signal.domain.models import Candle
from bybit_signal.providers.bybit import BybitTicker
from bybit_signal.providers.deep_market import BybitDeepMarketCollector


class FakePublicClient:
    def __init__(self) -> None:
        self.timeframes: list[str] = []

    async def tickers(self) -> dict[str, BybitTicker]:
        now = datetime.now(UTC)
        return {
            "CYSUSDT": BybitTicker(
                symbol="CYSUSDT",
                last_price=Decimal("1"),
                mark_price=Decimal("1.001"),
                bid_price=Decimal("0.999"),
                ask_price=Decimal("1.001"),
                high_24h=Decimal("1.2"),
                low_24h=Decimal("0.8"),
                turnover_24h=Decimal("1000000"),
                volume_24h=Decimal("1000000"),
                price_change_24h=Decimal("0.1"),
                observed_at=now,
            )
        }

    async def completed_candles(
        self, symbol: str, *, timeframe: str, limit: int
    ) -> tuple[Candle, ...]:
        self.timeframes.append(timeframe)
        duration = {
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "30m": timedelta(minutes=30),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
        }[timeframe]
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        end -= timedelta(seconds=end.timestamp() % duration.total_seconds())
        start = end - duration * 240
        return tuple(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + duration * index,
                close_time=start + duration * (index + 1),
                open=Decimal("1"),
                high=Decimal("1.1"),
                low=Decimal("0.9"),
                close=Decimal("1.01"),
                volume=Decimal("100"),
                turnover=Decimal("100"),
                completed=True,
                source="BYBIT",
            )
            for index in range(240)
        )

    async def orderbook(self, symbol: str, *, limit: int) -> Any:
        raise RuntimeError("optional book unavailable")

    async def recent_trades(self, symbol: str, *, limit: int) -> tuple[Any, ...]:
        return ()

    async def open_interest_history(
        self, symbol: str, *, interval: str, limit: int
    ) -> tuple[Any, ...]:
        return ()


@pytest.mark.asyncio
async def test_native_collector_fetches_all_required_timeframes_and_degrades_optional() -> None:
    client = FakePublicClient()
    snapshot = await BybitDeepMarketCollector(client).collect("CYSUSDT")  # type: ignore[arg-type]

    assert sorted(client.timeframes) == ["15m", "1h", "30m", "4h", "5m"]
    assert set(snapshot.candles) == {"5m", "15m", "30m", "1h", "4h"}
    assert snapshot.orderbook is None
    assert "orderbook" in snapshot.collection_failures
    assert all(
        candle.completed
        for candles in snapshot.candles.values()
        for candle in candles
    )
