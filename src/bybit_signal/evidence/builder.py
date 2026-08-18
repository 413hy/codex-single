from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import timedelta
from itertools import pairwise

from bybit_signal.domain.enums import PriceType, ToolStatus
from bybit_signal.domain.models import (
    Candle,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    ToolAssessment,
)
from bybit_signal.providers.bybit import BybitBookLevel, BybitOpenInterest
from bybit_signal.providers.deep_market import DeepTimeframe, NativeMarketSnapshot


class EvidenceBuilder:
    """Derive neutral, reproducible facts from this project's public data snapshot."""

    _TIMEFRAMES: tuple[DeepTimeframe, ...] = ("5m", "15m", "30m", "1h", "4h")

    def build(self, snapshot: NativeMarketSnapshot) -> EvidenceBundle:
        ticker = snapshot.ticker
        if ticker.mark_price is None:
            raise ValueError("canonical Bybit mark price is unavailable")
        last = CanonicalPrice(
            symbol=snapshot.symbol,
            price_type=PriceType.LAST,
            value=ticker.last_price,
            timestamp=ticker.observed_at,
        )
        mark = CanonicalPrice(
            symbol=snapshot.symbol,
            price_type=PriceType.MARK,
            value=ticker.mark_price,
            timestamp=ticker.observed_at,
        )
        items = [self._quality(snapshot), self._ticker(snapshot)]
        tools: list[ToolAssessment] = []

        price_action_ids: list[str] = []
        for timeframe in self._TIMEFRAMES:
            candles = snapshot.candles.get(timeframe)
            if candles:
                item = self._price_action(snapshot.symbol, timeframe, candles)
                items.append(item)
                price_action_ids.append(item.evidence_id)
        core_status = (
            ToolStatus.AVAILABLE
            if len(price_action_ids) == len(self._TIMEFRAMES)
            else ToolStatus.PARTIAL
        )
        core_ids = (
            f"{snapshot.symbol}.NATIVE.QUALITY",
            f"{snapshot.symbol}.BYBIT.TICKER",
            *price_action_ids,
        )
        tools.append(
            ToolAssessment(
                tool="BYBIT_NATIVE_MARKET_DATA",
                status=core_status,
                version="native-v1",
                reason=(
                    "self-contained public REST collector; last and mark remain separate; "
                    f"{len(price_action_ids)}/{len(self._TIMEFRAMES)} "
                    "completed-candle timeframes qualified"
                ),
                evidence_ids=core_ids,
            )
        )

        microstructure_items = self._microstructure(snapshot)
        items.extend(microstructure_items)
        tools.append(
            ToolAssessment(
                tool="BYBIT_NATIVE_MICROSTRUCTURE",
                status=(ToolStatus.AVAILABLE if microstructure_items else ToolStatus.UNAVAILABLE),
                version="native-v1",
                reason=(
                    "REST orderbook is point-in-time advisory evidence; public-trade delta "
                    "is directional only when the requested window is fully covered"
                ),
                evidence_ids=tuple(item.evidence_id for item in microstructure_items),
            )
        )

        derivatives = self._derivatives(snapshot)
        items.extend(derivatives)
        tools.append(
            ToolAssessment(
                tool="BYBIT_NATIVE_DERIVATIVES",
                status=ToolStatus.AVAILABLE if derivatives else ToolStatus.UNAVAILABLE,
                version="native-v1",
                reason="current funding/open interest and validated 5m OI history are advisory",
                evidence_ids=tuple(item.evidence_id for item in derivatives),
            )
        )
        references = self._reference_markets(snapshot)
        items.extend(references)
        tools.append(
            ToolAssessment(
                tool="NATIVE_CROSS_EXCHANGE_REFERENCE",
                status=ToolStatus.AVAILABLE if references else ToolStatus.UNAVAILABLE,
                version="native-v1",
                reason=(
                    "Binance Futures and OKX Swap are optional consistency checks; "
                    "their prices are never averaged into the canonical Bybit price"
                ),
                evidence_ids=tuple(item.evidence_id for item in references),
            )
        )
        return EvidenceBundle(
            symbol=snapshot.symbol,
            generated_at=snapshot.generated_at,
            source_snapshot_sha256=snapshot.sha256,
            canonical_last=last,
            canonical_mark=mark,
            evidence_items=tuple(items),
            tool_assessments=tuple(tools),
        )

    @staticmethod
    def _quality(snapshot: NativeMarketSnapshot) -> EvidenceItem:
        missing = sorted(snapshot.collection_failures)
        return EvidenceItem(
            evidence_id=f"{snapshot.symbol}.NATIVE.QUALITY",
            category="data_quality",
            source="BYBIT_NATIVE_COLLECTOR",
            observed_at=snapshot.generated_at,
            summary=(
                "All required public fields are identity checked; missing optional or "
                "under-warmed sources remain unknown rather than zero"
            ),
            values={
                "qualified_timeframe_count": len(snapshot.candles),
                "collection_duration_ms": round(
                    (snapshot.generated_at - snapshot.collection_started_at).total_seconds()
                    * 1000
                ),
                "evidence_cutoff_utc": snapshot.generated_at.isoformat(),
                "missing_source_count": len(missing),
                "missing_sources": ",".join(missing) if missing else "none",
                "orderbook_available": snapshot.orderbook is not None,
                "public_trade_count": len(snapshot.recent_trades),
                "open_interest_points": len(snapshot.open_interest),
            },
        )

    @staticmethod
    def _ticker(snapshot: NativeMarketSnapshot) -> EvidenceItem:
        ticker = snapshot.ticker
        return EvidenceItem(
            evidence_id=f"{snapshot.symbol}.BYBIT.TICKER",
            category="canonical_price",
            source="BYBIT_LINEAR_PERPETUAL",
            observed_at=ticker.observed_at,
            summary="Bybit linear-perpetual last, mark and index are preserved separately",
            values={
                "last_price": float(ticker.last_price),
                "mark_price": float(ticker.mark_price) if ticker.mark_price is not None else None,
                "index_price": (
                    float(ticker.index_price) if ticker.index_price is not None else None
                ),
                "last_mark_basis_bps": (
                    float((ticker.last_price - ticker.mark_price) / ticker.mark_price * 10_000)
                    if ticker.mark_price is not None
                    else None
                ),
                "bid_price": float(ticker.bid_price),
                "ask_price": float(ticker.ask_price),
                "spread_bps": float(ticker.spread_bps),
                "range_24h_percent": float(ticker.range_24h_percent),
                "price_change_24h_percent": float(ticker.price_change_24h * 100),
                "turnover_24h": float(ticker.turnover_24h),
            },
        )

    def _price_action(
        self,
        symbol: str,
        timeframe: str,
        candles: Sequence[Candle],
    ) -> EvidenceItem:
        values = tuple(candles[-240:])
        closes = [float(candle.close) for candle in values]
        opens = [float(candle.open) for candle in values]
        highs = [float(candle.high) for candle in values]
        lows = [float(candle.low) for candle in values]
        turnovers = [float(candle.turnover) for candle in values]
        atr14 = _atr(highs, lows, closes, 14)
        ema12_series = _ema_series(closes, 12)
        ema26_series = _ema_series(closes, 26)
        macd_series = [fast - slow for fast, slow in zip(ema12_series, ema26_series, strict=True)]
        signal_series = _ema_series(macd_series, 9)
        lookback = min(12, len(values))
        pivot_high, pivot_high_at, pivot_high_confirmed, pivot_high_age = _last_pivot(
            values, high=True
        )
        pivot_low, pivot_low_at, pivot_low_confirmed, pivot_low_age = _last_pivot(
            values, high=False
        )
        recent_turnover = sum(turnovers[-6:])
        prior_turnover = sum(turnovers[-12:-6]) if len(turnovers) >= 12 else 0.0
        rolling_high = max(highs[-20:])
        rolling_low = min(lows[-20:])
        evidence_values: dict[str, str | int | float | bool | None] = {
            "completed_candles": len(values),
            "latest_completed_close_time": values[-1].close_time.isoformat(),
            "latest_close": closes[-1],
            "return_1_percent": _return_percent(closes[-2], closes[-1]),
            "return_3_percent": _return_percent(closes[-4], closes[-1]),
            "return_12_percent": _return_percent(closes[-13], closes[-1]),
            "atr_14": atr14,
            "atr_14_percent": atr14 / closes[-1] * 100,
            "ema_9": _ema_series(closes, 9)[-1],
            "ema_20": _ema_series(closes, 20)[-1],
            "ema_50": _ema_series(closes, 50)[-1],
            "rsi_14": _rsi(closes, 14),
            "macd_histogram_12_26_9": macd_series[-1] - signal_series[-1],
            "rolling_high_20": rolling_high,
            "rolling_low_20": rolling_low,
            "range_mid_20": (rolling_high + rolling_low) / 2,
            "drawdown_from_rolling_high_atr": max(
                0.0, (rolling_high - closes[-1]) / atr14
            ),
            "rebound_from_rolling_low_atr": max(
                0.0, (closes[-1] - rolling_low) / atr14
            ),
            "directional_efficiency_12": _directional_efficiency(closes[-lookback:]),
            "overlap_ratio_12": _overlap_ratio(highs[-lookback:], lows[-lookback:]),
            "bull_body_ratio_12": sum(
                close > open_
                for close, open_ in zip(
                    closes[-lookback:], opens[-lookback:], strict=True
                )
            )
            / lookback,
            "turnover_ratio_6_vs_6": (
                recent_turnover / prior_turnover if prior_turnover > 0 else None
            ),
            "recent_30m_turnover_usdt": recent_turnover,
            "confirmed_pivot_high": pivot_high,
            "pivot_high_open_time": pivot_high_at,
            "pivot_high_confirmed_at": pivot_high_confirmed,
            "pivot_high_age_bars": pivot_high_age,
            "confirmed_pivot_low": pivot_low,
            "pivot_low_open_time": pivot_low_at,
            "pivot_low_confirmed_at": pivot_low_confirmed,
            "pivot_low_age_bars": pivot_low_age,
        }
        return EvidenceItem(
            evidence_id=f"{symbol}.PA.{timeframe.upper()}",
            category="price_action",
            source="BYBIT_NATIVE_COMPLETED_CANDLES",
            observed_at=values[-1].close_time,
            summary=(
                f"{timeframe}: {len(values)} completed candles; latest close "
                f"{closes[-1]:g}; ATR14 {atr14:g}; forming candle excluded"
            ),
            values=evidence_values,
        )

    def _microstructure(self, snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        book = snapshot.orderbook
        if book is not None:
            bid5 = _notional(book.bids[:5])
            ask5 = _notional(book.asks[:5])
            bid20 = _notional(book.bids[:20])
            ask20 = _notional(book.asks[:20])
            midpoint = (book.bids[0].price + book.asks[0].price) / 2
            items.append(
                EvidenceItem(
                    evidence_id=f"{snapshot.symbol}.MICRO.BOOK",
                    category="orderbook",
                    source="BYBIT_PUBLIC_REST_ORDERBOOK",
                    observed_at=book.observed_at,
                    summary=(
                        "Point-in-time REST book; imbalance is advisory and does not claim "
                        "WebSocket sequence continuity"
                    ),
                    values={
                        "spread_bps": float(
                            (book.asks[0].price - book.bids[0].price) / midpoint * 10_000
                        ),
                        "imbalance_top5": _imbalance(bid5, ask5),
                        "imbalance_top20": _imbalance(bid20, ask20),
                        "bid_notional_top20": bid20,
                        "ask_notional_top20": ask20,
                        "update_id": book.update_id,
                        "sequence": book.sequence,
                    },
                )
            )
        if snapshot.recent_trades:
            for minutes in (1, 5):
                items.append(self._trade_window(snapshot, minutes))
        return items

    @staticmethod
    def _trade_window(snapshot: NativeMarketSnapshot, minutes: int) -> EvidenceItem:
        cutoff = snapshot.generated_at - timedelta(minutes=minutes)
        trades = [trade for trade in snapshot.recent_trades if trade.timestamp >= cutoff]
        oldest = snapshot.recent_trades[0].timestamp
        newest = snapshot.recent_trades[-1].timestamp
        qualified = oldest <= cutoff and newest >= snapshot.generated_at - timedelta(seconds=30)
        buy = sum(float(trade.notional) for trade in trades if trade.side == "Buy")
        sell = sum(float(trade.notional) for trade in trades if trade.side == "Sell")
        total = buy + sell
        return EvidenceItem(
            evidence_id=f"{snapshot.symbol}.MICRO.TRADES.{minutes}M",
            category="order_flow",
            source="BYBIT_PUBLIC_RECENT_TRADES",
            observed_at=newest,
            summary=(
                f"{minutes}m trade window is fully covered"
                if qualified
                else f"{minutes}m trade window coverage is incomplete; delta excluded"
            ),
            values={
                "qualified": qualified,
                "sample_count": len(trades),
                "oldest_trade_time": oldest.isoformat(),
                "newest_trade_time": newest.isoformat(),
                "buy_notional": buy if qualified else None,
                "sell_notional": sell if qualified else None,
                "normalized_delta": (buy - sell) / total if qualified and total else None,
                "buy_sell_ratio": buy / sell if qualified and sell > 0 else None,
            },
        )

    @staticmethod
    def _derivatives(snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        ticker = snapshot.ticker
        if (
            ticker.funding_rate is None
            and ticker.open_interest is None
            and not snapshot.open_interest
        ):
            return []
        points = snapshot.open_interest
        return [
            EvidenceItem(
                evidence_id=f"{snapshot.symbol}.DERIVATIVES",
                category="derivatives",
                source="BYBIT_PUBLIC_DERIVATIVES",
                observed_at=ticker.observed_at,
                summary="Funding and open interest are context, never a standalone direction",
                values={
                    "funding_rate": (
                        float(ticker.funding_rate) if ticker.funding_rate is not None else None
                    ),
                    "current_open_interest": (
                        float(ticker.open_interest)
                        if ticker.open_interest is not None
                        else None
                    ),
                    "current_open_interest_value": (
                        float(ticker.open_interest_value)
                        if ticker.open_interest_value is not None
                        else None
                    ),
                    "oi_history_points": len(points),
                    "oi_change_15m_percent": _oi_change(points, 3),
                    "oi_change_1h_percent": _oi_change(points, 12),
                    "next_funding_time": (
                        ticker.next_funding_time.isoformat()
                        if ticker.next_funding_time is not None
                        else None
                    ),
                },
            )
        ]

    @staticmethod
    def _reference_markets(snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        bybit_last = snapshot.ticker.last_price
        items: list[EvidenceItem] = []
        for ticker in snapshot.reference_tickers:
            items.append(
                EvidenceItem(
                    evidence_id=f"{snapshot.symbol}.REFERENCE.{ticker.exchange}",
                    category="cross_exchange_reference",
                    source=f"{ticker.exchange}_PUBLIC_PERPETUAL",
                    observed_at=ticker.observed_at,
                    summary=(
                        f"{ticker.exchange} is an optional price-consistency reference; "
                        "Bybit remains canonical"
                    ),
                    values={
                        "instrument": ticker.instrument,
                        "last_price": float(ticker.last_price),
                        "bid_price": float(ticker.bid_price),
                        "ask_price": float(ticker.ask_price),
                        "spread_bps": float(ticker.spread_bps),
                        "last_divergence_vs_bybit_bps": float(
                            (ticker.last_price - bybit_last) / bybit_last * 10_000
                        ),
                    },
                )
            )
        return items


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError(f"EMA{period} needs at least {period} values")
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> float:
    if len(closes) <= period:
        raise ValueError("ATR window is too short")
    true_ranges = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    atr = statistics.fmean(true_ranges[1 : period + 1])
    for value in true_ranges[period + 1 :]:
        atr = (atr * (period - 1) + value) / period
    return atr


def _rsi(closes: Sequence[float], period: int) -> float:
    changes = [current - previous for previous, current in pairwise(closes)]
    if len(changes) < period:
        raise ValueError("RSI window is too short")
    gain = statistics.fmean(max(change, 0) for change in changes[:period])
    loss = statistics.fmean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _return_percent(start: float, end: float) -> float:
    return (end / start - 1) * 100


def _directional_efficiency(closes: Sequence[float]) -> float:
    path = sum(abs(current - previous) for previous, current in pairwise(closes))
    return (closes[-1] - closes[0]) / path if path else 0.0


def _overlap_ratio(highs: Sequence[float], lows: Sequence[float]) -> float:
    overlaps: list[float] = []
    for index in range(1, len(highs)):
        prior_high, prior_low = highs[index - 1], lows[index - 1]
        high, low = highs[index], lows[index]
        union = max(prior_high, high) - min(prior_low, low)
        overlap = max(0.0, min(prior_high, high) - max(prior_low, low))
        overlaps.append(overlap / union if union else 0.0)
    return statistics.fmean(overlaps) if overlaps else 0.0


def _last_pivot(
    candles: Sequence[Candle], *, high: bool
) -> tuple[float | None, str | None, str | None, int | None]:
    field = "high" if high else "low"
    for index in range(len(candles) - 3, 1, -1):
        value = getattr(candles[index], field)
        neighbors = [
            getattr(candles[position], field)
            for position in range(index - 2, index + 3)
        ]
        if (high and value == max(neighbors)) or (not high and value == min(neighbors)):
            return (
                float(value),
                candles[index].open_time.isoformat(),
                candles[index + 2].close_time.isoformat(),
                len(candles) - index - 3,
            )
    return None, None, None, None


def _notional(levels: Sequence[BybitBookLevel]) -> float:
    return sum(float(level.notional) for level in levels)


def _imbalance(bid: float, ask: float) -> float | None:
    total = bid + ask
    return (bid - ask) / total if total else None


def _oi_change(points: Sequence[BybitOpenInterest], periods: int) -> float | None:
    if len(points) <= periods:
        return None
    current = float(points[-1].open_interest)
    previous = float(points[-1 - periods].open_interest)
    return (current / previous - 1) * 100 if previous > 0 else None
