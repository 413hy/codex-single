from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import pairwise
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analysis_core.vendor.signal.domain.market_requirements import PRICE_ACTION_MINIMUM_CANDLES
from analysis_core.vendor.signal.domain.models import Candle, MarketContext, Symbol
from analysis_core.vendor.signal.providers.bybit import (
    BybitLongShortRatio,
    BybitOpenInterest,
    BybitOrderBook,
    BybitPublicClient,
    BybitPublicTrade,
    BybitTicker,
)
from analysis_core.vendor.signal.providers.cross_exchange import (
    CrossExchangePublicClient,
    ReferenceTicker,
)
from analysis_core.vendor.signal.providers.market_context import MarketContextCollector
from analysis_core.vendor.signal.providers.public_streams import (
    LiquidationWindow,
    PublicStreamCache,
)

DeepTimeframe = Literal["1m", "3m", "5m", "15m", "30m", "1h", "4h"]
DeepCandleSet = dict[DeepTimeframe, tuple[Candle, ...]]
T = TypeVar("T")


class NativeMarketSnapshot(BaseModel):
    """Validated public-market input produced entirely inside this project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: Symbol
    collection_started_at: datetime
    generated_at: datetime
    ticker: BybitTicker
    candles: DeepCandleSet
    orderbook: BybitOrderBook | None = None
    recent_trades: tuple[BybitPublicTrade, ...] = ()
    open_interest: tuple[BybitOpenInterest, ...] = ()
    long_short_ratios: tuple[BybitLongShortRatio, ...] = ()
    liquidation_window: LiquidationWindow | None = None
    liquidation_window_1m: LiquidationWindow | None = None
    reference_tickers: tuple[ReferenceTicker, ...] = ()
    market_context: MarketContext | None = None
    collection_failures: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> NativeMarketSnapshot:
        for field, value in (
            ("collection_started_at", self.collection_started_at),
            ("generated_at", self.generated_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field} must be timezone-aware")
        if self.collection_started_at > self.generated_at:
            raise ValueError("collection starts after the frozen evidence cutoff")
        related: Sequence[object] = (
            self.ticker,
            *self.recent_trades,
            *self.open_interest,
            *self.long_short_ratios,
            *self.reference_tickers,
            *(candle for values in self.candles.values() for candle in values),
        )
        if self.orderbook is not None:
            related = (*related, self.orderbook)
        if any(getattr(item, "symbol", None) != self.symbol for item in related):
            raise ValueError("native market snapshot contains another symbol")
        if any(not candle.completed for candles in self.candles.values() for candle in candles):
            raise ValueError("native market snapshot contains a forming candle")
        timestamps = [
            self.ticker.observed_at,
            *(trade.timestamp for trade in self.recent_trades),
            *(point.timestamp for point in self.open_interest),
            *(ticker.observed_at for ticker in self.reference_tickers),
            *(candle.close_time for values in self.candles.values() for candle in values),
        ]
        if self.orderbook is not None:
            timestamps.append(self.orderbook.observed_at)
        tolerance = timedelta(seconds=2)
        if any(timestamp > self.generated_at + tolerance for timestamp in timestamps):
            raise ValueError("snapshot contains evidence after its frozen cutoff")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class BybitDeepMarketCollector:
    """Build a compact, auditable evidence snapshot from Bybit public endpoints."""

    _TIMEFRAMES: tuple[DeepTimeframe, ...] = (
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "4h",
    )
    _MINIMUM_CANDLES: ClassVar[dict[DeepTimeframe, int]] = {
        "3m": 120,
        "5m": 120,
        "15m": 96,
        "30m": 72,
        "1h": 72,
        "4h": PRICE_ACTION_MINIMUM_CANDLES,
    }
    _DURATIONS: ClassVar[dict[DeepTimeframe, timedelta]] = {
        "3m": timedelta(minutes=3),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }

    def __init__(
        self,
        client: BybitPublicClient,
        *,
        reference_client: CrossExchangePublicClient | None = None,
        candle_limit: int = 240,
        request_concurrency: int = 8,
        market_context_collector: MarketContextCollector | None = None,
        stream_cache: PublicStreamCache | None = None,
    ) -> None:
        self._client = client
        self._reference_client = reference_client
        self._candle_limit = candle_limit
        self._semaphore = asyncio.Semaphore(request_concurrency)
        self._stream_cache = stream_cache
        self._market_context_collector = market_context_collector or MarketContextCollector(
            client, stream_cache=stream_cache
        )

    async def collect_many(
        self, symbols: Sequence[str]
    ) -> tuple[dict[str, NativeMarketSnapshot], dict[str, str]]:
        tickers = await self._client.tickers()
        snapshots: dict[str, NativeMarketSnapshot] = {}
        failures: dict[str, str] = {}

        async def collect_one(symbol: str) -> None:
            ticker = tickers.get(symbol)
            if ticker is None or ticker.mark_price is None:
                failures[symbol] = "Bybit ticker or mark price is unavailable"
                return
            try:
                snapshots[symbol] = await self.collect(symbol, ticker=ticker)
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"

        await asyncio.gather(*(collect_one(symbol) for symbol in symbols))
        candidate_candles = {
            symbol: snapshot.candles.get("5m", ()) for symbol, snapshot in snapshots.items()
        }
        try:
            context = await self._market_context_collector.collect(
                tickers=tickers,
                candidate_candles=candidate_candles,
            )
        except Exception as error:
            context = None
            failures["MARKET_CONTEXT"] = f"{type(error).__name__}: {error}"
        if context is not None:
            snapshots = {
                symbol: snapshot.model_copy(
                    update={
                        "market_context": context,
                        "generated_at": max(snapshot.generated_at, context.generated_at),
                    }
                )
                for symbol, snapshot in snapshots.items()
            }
        return snapshots, failures

    async def collect(
        self,
        symbol: str,
        *,
        ticker: BybitTicker | None = None,
    ) -> NativeMarketSnapshot:
        if ticker is None:
            ticker = (await self._client.tickers()).get(symbol)
        if ticker is None or ticker.mark_price is None:
            raise ValueError(f"{symbol} has no valid Bybit last/mark ticker")
        started_at = datetime.now(UTC)
        if abs((started_at - ticker.observed_at).total_seconds()) > 60:
            raise ValueError(f"{symbol} Bybit ticker is stale")

        async def fetch_candles(timeframe: DeepTimeframe) -> tuple[Candle, ...]:
            return await self._client.completed_candles(
                symbol,
                timeframe=timeframe,
                limit=self._candle_limit,
            )

        candle_calls = [
            self._safe(f"candles.{timeframe}", partial(fetch_candles, timeframe))
            for timeframe in self._TIMEFRAMES
        ]
        orderbook_task = asyncio.create_task(
            self._safe("orderbook", lambda: self._client.orderbook(symbol, limit=50))
        )
        trades_task = asyncio.create_task(
            self._safe("recent_trades", lambda: self._client.recent_trades(symbol, limit=1000))
        )
        oi_task = asyncio.create_task(
            self._safe(
                "open_interest",
                lambda: self._client.open_interest_history(
                    symbol,
                    interval="5min",
                    limit=48,
                ),
            )
        )
        ratio_task = asyncio.create_task(
            self._safe(
                "long_short_ratio",
                lambda: self._client.long_short_ratio(
                    symbol,
                    period="5min",
                    limit=48,
                ),
            )
        )
        reference_task = (
            asyncio.create_task(self._reference_client.tickers(symbol))
            if self._reference_client is not None
            else None
        )
        candle_results = await asyncio.gather(*candle_calls)
        failures: dict[str, str] = {}
        candles: DeepCandleSet = {}
        for timeframe, result in zip(self._TIMEFRAMES, candle_results, strict=True):
            value, error = result
            if error is not None or value is None:
                failures[f"candles.{timeframe}"] = error or "empty candle result"
                continue
            # A request can finish just after a natural candle boundary. Preserve
            # the collection-start cutoff by trimming newly completed rows instead
            # of discarding the entire timeframe as internally inconsistent.
            values = tuple(candle for candle in value if candle.close_time <= started_at)
            quality_error = self._candle_quality_error(timeframe, values, started_at)
            if quality_error is not None:
                failures[f"candles.{timeframe}"] = quality_error
                continue
            candles[timeframe] = values

        orderbook_result, trades_result, oi_result, ratio_result = await asyncio.gather(
            orderbook_task, trades_task, oi_task, ratio_task
        )
        orderbook = self._optional_value("orderbook", orderbook_result, failures)
        trades = self._optional_value("recent_trades", trades_result, failures) or ()
        open_interest = self._optional_value("open_interest", oi_result, failures) or ()
        long_short_ratios = self._optional_value("long_short_ratio", ratio_result, failures) or ()
        liquidation_window = (
            self._stream_cache.liquidation_window(symbol, seconds=300)
            if self._stream_cache is not None
            else None
        )
        reference_tickers: tuple[ReferenceTicker, ...] = ()
        if reference_task is not None:
            reference_tickers, reference_failures = await reference_task
            failures.update(reference_failures)
            fresh_references = []
            for reference in reference_tickers:
                age = abs((started_at - reference.observed_at).total_seconds())
                if age <= 300:
                    fresh_references.append(reference)
                else:
                    failures[f"reference.{reference.exchange.lower()}"] = (
                        f"ticker is {age:.0f}s stale"
                    )
            reference_tickers = tuple(fresh_references)
        return NativeMarketSnapshot(
            symbol=symbol,
            collection_started_at=started_at,
            generated_at=datetime.now(UTC),
            ticker=ticker,
            candles=candles,
            orderbook=orderbook,
            recent_trades=tuple(trades),
            open_interest=tuple(open_interest),
            long_short_ratios=tuple(long_short_ratios),
            liquidation_window=liquidation_window,
            liquidation_window_1m=None,
            reference_tickers=reference_tickers,
            collection_failures=failures,
        )

    def _candle_quality_error(
        self,
        timeframe: DeepTimeframe,
        candles: tuple[Candle, ...],
        observed_at: datetime,
    ) -> str | None:
        minimum = self._MINIMUM_CANDLES[timeframe]
        if len(candles) < minimum:
            return f"only {len(candles)} completed candles; minimum is {minimum}"
        duration = self._DURATIONS[timeframe]
        if any(
            current.open_time - previous.open_time != duration
            for previous, current in pairwise(candles)
        ):
            return "completed-candle window has a missing or duplicated interval"
        if candles[-1].close_time > observed_at:
            return "latest candle is still forming"
        age = (observed_at - candles[-1].close_time).total_seconds()
        if age > duration.total_seconds() * 2:
            return f"latest completed candle is {age:.0f}s stale"
        return None

    async def _safe(
        self,
        _name: str,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[T | None, str | None]:
        async with self._semaphore:
            try:
                return await operation(), None
            except Exception as error:
                return None, f"{type(error).__name__}: {error}"

    @staticmethod
    def _optional_value(
        name: str,
        result: tuple[Any | None, str | None],
        failures: dict[str, str],
    ) -> Any | None:
        value, error = result
        if error is not None:
            failures[name] = error
            return None
        return value
