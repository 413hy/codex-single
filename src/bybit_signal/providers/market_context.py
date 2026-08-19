from __future__ import annotations

import asyncio
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from bybit_signal.domain.enums import ToolStatus
from bybit_signal.domain.models import BreadthSnapshot, Candle, MarketContext
from bybit_signal.providers.bybit import BybitPublicClient, BybitTicker
from bybit_signal.providers.public_streams import PublicStreamCache


class MarketContextCollector:
    """Builds a bounded market-wide context from public Bybit facts."""

    def __init__(
        self,
        client: BybitPublicClient,
        *,
        stream_cache: PublicStreamCache | None = None,
    ) -> None:
        self._client = client
        self._stream_cache = stream_cache

    async def collect(
        self,
        *,
        tickers: Mapping[str, BybitTicker] | None = None,
        candidate_candles: Mapping[str, Sequence[Candle]] | None = None,
    ) -> MarketContext:
        ticker_map = dict(tickers or await self._client.tickers())
        generated_at = datetime.now(UTC)
        changes = [float(ticker.price_change_24h * 100) for ticker in ticker_map.values()]
        advancing = sum(value > 0 for value in changes)
        declining = sum(value < 0 for value in changes)
        turnover_total = sum(float(ticker.turnover_24h) for ticker in ticker_map.values())
        positive_turnover = sum(
            float(ticker.turnover_24h)
            for ticker in ticker_map.values()
            if ticker.price_change_24h > 0
        )
        top_turnover = sum(
            sorted(
                (float(ticker.turnover_24h) for ticker in ticker_map.values()),
                reverse=True,
            )[:10]
        )
        btc, eth = await asyncio.gather(
            self._benchmark_returns("BTCUSDT"),
            self._benchmark_returns("ETHUSDT"),
        )
        candidate_returns = []
        if candidate_candles:
            for candles in candidate_candles.values():
                if len(candles) >= 2:
                    candidate_returns.append(
                        (float(candles[-1].close / candles[-2].close) - 1) * 100
                    )
        synchronization = _sign_synchronization(candidate_returns)
        liquidation_status = ToolStatus.WARMING_UP
        liquidation_notional: Decimal | None = None
        if self._stream_cache is not None:
            windows = [
                self._stream_cache.liquidation_window(symbol, seconds=300, now=generated_at)
                for symbol in ("BTCUSDT", "ETHUSDT")
            ]
            available = [window for window in windows if window.event_count is not None]
            if available:
                liquidation_status = (
                    ToolStatus.AVAILABLE
                    if all(window.coverage_complete for window in available)
                    else ToolStatus.PARTIAL
                )
                liquidation_notional = sum(
                    (
                        (window.long_notional or Decimal(0)) + (window.short_notional or Decimal(0))
                        for window in available
                    ),
                    Decimal(0),
                )
        limitations = []
        if liquidation_status is not ToolStatus.AVAILABLE:
            limitations.append("market_liquidation_stream_not_fully_warmed")
        return MarketContext(
            generated_at=generated_at,
            breadth=BreadthSnapshot(
                observed_at=max(
                    (ticker.observed_at for ticker in ticker_map.values()),
                    default=generated_at,
                ),
                instrument_count=len(changes),
                advancing_count=advancing,
                declining_count=declining,
                unchanged_count=len(changes) - advancing - declining,
                median_change_24h_percent=statistics.median(changes) if changes else None,
                positive_turnover_share=(
                    positive_turnover / turnover_total if turnover_total > 0 else None
                ),
                top_turnover_share=top_turnover / turnover_total if turnover_total > 0 else None,
            ),
            btc_returns_percent=btc,
            eth_returns_percent=eth,
            candidate_median_return_5m_percent=(
                statistics.median(candidate_returns) if candidate_returns else None
            ),
            candidate_synchronization=synchronization,
            liquidation_status=liquidation_status,
            liquidation_notional_5m=liquidation_notional,
            limitations=tuple(limitations),
        )

    async def _benchmark_returns(self, symbol: str) -> dict[str, float | None]:
        timeframes: tuple[Literal["1m", "5m", "15m"], ...] = ("1m", "5m", "15m")
        results = await asyncio.gather(
            *(
                self._client.completed_candles(symbol, timeframe=timeframe, limit=5)
                for timeframe in timeframes
            ),
            return_exceptions=True,
        )
        values: dict[str, float | None] = {}
        for timeframe, result in zip(timeframes, results, strict=True):
            if isinstance(result, BaseException) or len(result) < 2:
                values[timeframe] = None
            else:
                values[timeframe] = (float(result[-1].close / result[-2].close) - 1) * 100
        return values


def _sign_synchronization(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    return (max(positive, negative) / len(values) * 2) - 1
