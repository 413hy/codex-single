from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from bybit_signal.domain.enums import ToolStatus
from bybit_signal.domain.models import (
    AnalysisToolRequest,
    AnalysisToolResult,
    EvidenceItem,
)
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.providers.cross_exchange import CrossExchangePublicClient
from bybit_signal.providers.market_context import MarketContextCollector
from bybit_signal.storage.sqlite import SignalStore


class ReadOnlyAnalysisToolRegistry:
    """Host-owned, bounded public/read-only tools available to the model."""

    def __init__(
        self,
        *,
        bybit: BybitPublicClient,
        market_context: MarketContextCollector,
        store: SignalStore,
        reference_markets: CrossExchangePublicClient | None = None,
        timeout_seconds: int = 12,
    ) -> None:
        self._bybit = bybit
        self._market_context = market_context
        self._store = store
        self._reference_markets = reference_markets
        self._timeout_seconds = timeout_seconds

    async def execute_many(
        self,
        requests: Sequence[AnalysisToolRequest],
        *,
        allowed_symbols: frozenset[str],
    ) -> tuple[AnalysisToolResult, ...]:
        request_ids = [request.request_id for request in requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("tool request ids must be unique")
        return tuple(
            await asyncio.gather(
                *(self._execute(request, allowed_symbols=allowed_symbols) for request in requests)
            )
        )

    async def _execute(
        self,
        request: AnalysisToolRequest,
        *,
        allowed_symbols: frozenset[str],
    ) -> AnalysisToolResult:
        requested_at = datetime.now(UTC)
        started = time.monotonic()
        if request.symbol is not None and request.symbol not in allowed_symbols:
            return self._error(
                request,
                requested_at,
                started,
                "symbol is outside this analysis batch",
            )
        if request.tool != "market_context" and request.symbol is None:
            return self._error(
                request,
                requested_at,
                started,
                "this tool requires a symbol",
            )
        try:
            items = await asyncio.wait_for(self._dispatch(request), timeout=self._timeout_seconds)
            return AnalysisToolResult(
                request_id=request.request_id,
                tool=request.tool,
                symbol=request.symbol,
                status=ToolStatus.AVAILABLE if items else ToolStatus.UNAVAILABLE,
                requested_at=requested_at,
                completed_at=datetime.now(UTC),
                latency_ms=round((time.monotonic() - started) * 1000),
                evidence_items=items,
            )
        except Exception as error:
            return self._error(
                request,
                requested_at,
                started,
                f"{type(error).__name__}: {error}",
            )

    async def _dispatch(self, request: AnalysisToolRequest) -> tuple[EvidenceItem, ...]:
        symbol = request.symbol
        evidence_id = _evidence_id(request)
        if request.tool == "market_context":
            context = await self._market_context.collect()
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_market_context",
                    source="HOST_READ_ONLY_TOOL",
                    observed_at=context.generated_at,
                    summary="Fresh Bybit breadth and BTC/ETH public-market context",
                    values=context.model_dump(mode="json"),
                ),
            )
        if symbol is None:
            raise ValueError("tool requires a symbol")
        if request.tool == "latest_market":
            ticker = (await self._bybit.tickers()).get(symbol)
            if ticker is None:
                return ()
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_latest_market",
                    source="BYBIT_PUBLIC_TICKER",
                    observed_at=ticker.observed_at,
                    summary="Fresh Bybit last/mark/index and top-of-book ticker",
                    values=ticker.model_dump(mode="json"),
                ),
            )
        if request.tool in {"short_candles", "extended_candles"}:
            default_limit = 60 if request.tool == "short_candles" else 240
            limit = request.arguments.limit or default_limit
            limit = max(5, min(limit, 240))
            requested_frames = request.arguments.timeframes or "1m,5m"
            allowed_frames = ("1m", "5m", "15m", "30m", "1h", "4h")
            frames = tuple(
                frame for frame in requested_frames.split(",") if frame in allowed_frames
            )[:3]
            if not frames:
                frames = ("1m", "5m")
            results = await asyncio.gather(
                *(
                    self._bybit.completed_candles(
                        symbol,
                        timeframe=frame,  # type: ignore[arg-type]
                        limit=limit,
                    )
                    for frame in frames
                )
            )
            items = []
            for index, (frame, candles) in enumerate(zip(frames, results, strict=True), start=1):
                if not candles:
                    continue
                items.append(
                    EvidenceItem(
                        evidence_id=f"{evidence_id}.{index}",
                        category="tool_candles",
                        source="BYBIT_PUBLIC_COMPLETED_CANDLES",
                        observed_at=candles[-1].close_time,
                        summary=f"{len(candles)} completed {frame} candles",
                        values={
                            "timeframe": frame,
                            "rows": [candle.model_dump(mode="json") for candle in candles[-60:]],
                        },
                    )
                )
            return tuple(items)
        if request.tool == "depth_and_trades":
            book, trades = await asyncio.gather(
                self._bybit.orderbook(symbol, limit=50),
                self._bybit.recent_trades(symbol, limit=1000),
            )
            bid20 = sum((level.notional for level in book.bids[:20]), Decimal(0))
            ask20 = sum((level.notional for level in book.asks[:20]), Decimal(0))
            buy = sum((trade.notional for trade in trades if trade.side == "Buy"), Decimal(0))
            sell = sum((trade.notional for trade in trades if trade.side == "Sell"), Decimal(0))
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_depth_trades",
                    source="BYBIT_PUBLIC_BOOK_AND_TRADES",
                    observed_at=max(book.observed_at, trades[-1].timestamp),
                    summary="Fresh point-in-time depth and bounded recent public trades",
                    values={
                        "bid_notional_top20": bid20,
                        "ask_notional_top20": ask20,
                        "trade_count": len(trades),
                        "buy_notional": buy,
                        "sell_notional": sell,
                        "coverage_start": trades[0].timestamp.isoformat(),
                        "coverage_end": trades[-1].timestamp.isoformat(),
                    },
                ),
            )
        if request.tool == "derivatives":
            tickers, oi, ratio = await asyncio.gather(
                self._bybit.tickers(),
                self._bybit.open_interest_history(symbol, interval="5min", limit=48),
                self._bybit.long_short_ratio(symbol, period="5min", limit=48),
            )
            ticker = tickers.get(symbol)
            observed_at = max(
                ticker.observed_at if ticker is not None else datetime.min.replace(tzinfo=UTC),
                oi[-1].timestamp if oi else datetime.min.replace(tzinfo=UTC),
                ratio[-1].timestamp if ratio else datetime.min.replace(tzinfo=UTC),
            )
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_derivatives",
                    source="BYBIT_PUBLIC_DERIVATIVES",
                    observed_at=observed_at,
                    summary="Fresh funding, OI history and public account ratio",
                    values={
                        "ticker": ticker.model_dump(mode="json") if ticker else None,
                        "open_interest": [point.model_dump(mode="json") for point in oi],
                        "account_ratio": [point.model_dump(mode="json") for point in ratio],
                    },
                ),
            )
        if request.tool == "cross_exchange":
            if self._reference_markets is None:
                return ()
            reference_tickers, failures = await self._reference_markets.tickers(symbol)
            if not reference_tickers:
                return ()
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_cross_exchange",
                    source="PUBLIC_REFERENCE_EXCHANGES",
                    observed_at=max(ticker.observed_at for ticker in reference_tickers),
                    summary="Binance/OKX consistency only; Bybit remains canonical",
                    values={
                        "tickers": [ticker.model_dump(mode="json") for ticker in reference_tickers],
                        "failures": failures,
                    },
                ),
            )
        if request.tool == "signal_history":
            limit = max(1, min(request.arguments.limit or 10, 20))
            history = await self._store.signal_history(symbol, limit=limit)
            if not history:
                return ()
            return (
                EvidenceItem(
                    evidence_id=evidence_id,
                    category="tool_signal_history",
                    source="LOCAL_READ_ONLY_AUDIT",
                    observed_at=history[0].generated_at,
                    summary="Historical outputs are audit context, never current market facts",
                    values={
                        "history": [
                            {
                                "analysis_id": item.analysis_id,
                                "generated_at": item.generated_at.isoformat(),
                                "direction": (
                                    item.assessment.direction.value
                                    if item.assessment.direction is not None
                                    else None
                                ),
                                "tracking_status": item.tracking_status.value,
                            }
                            for item in history
                        ]
                    },
                ),
            )
        raise ValueError("tool is not registered")

    @staticmethod
    def _error(
        request: AnalysisToolRequest,
        requested_at: datetime,
        started: float,
        message: str,
    ) -> AnalysisToolResult:
        compact = message.replace("\r", " ").replace("\n", " ")[:500]
        return AnalysisToolResult(
            request_id=request.request_id,
            tool=request.tool,
            symbol=request.symbol,
            status=ToolStatus.ERROR,
            requested_at=requested_at,
            completed_at=datetime.now(UTC),
            latency_ms=round((time.monotonic() - started) * 1000),
            error=compact,
        )


def _evidence_id(request: AnalysisToolRequest) -> str:
    request_id = re.sub(r"[^A-Z0-9_.:-]", "_", request.request_id.upper())
    symbol = request.symbol or "GLOBAL"
    return f"{symbol}.TOOL.{request.tool.upper()}.{request_id}"[:127]
