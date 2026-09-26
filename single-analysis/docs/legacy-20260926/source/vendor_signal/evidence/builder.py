from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import timedelta
from itertools import pairwise

from analysis_core.vendor.scanner import RankedCandidate
from analysis_core.vendor.signal.domain.enums import CandidateRiskTag, PriceType, ToolStatus
from analysis_core.vendor.signal.domain.market_requirements import PRICE_ACTION_MINIMUM_CANDLES
from analysis_core.vendor.signal.domain.models import (
    CandidateContext,
    Candle,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    ToolAssessment,
)
from analysis_core.vendor.signal.providers.bybit import BybitBookLevel, BybitOpenInterest
from analysis_core.vendor.signal.providers.deep_market import DeepTimeframe, NativeMarketSnapshot


class EvidenceBuilder:
    """Derive neutral, reproducible facts from this project's public data snapshot."""

    _TIMEFRAMES: tuple[DeepTimeframe, ...] = (
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "4h",
    )

    def build(
        self,
        snapshot: NativeMarketSnapshot,
        candidate: RankedCandidate | None = None,
    ) -> EvidenceBundle:
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
        under_warmed_timeframes = tuple(
            timeframe
            for timeframe in self._TIMEFRAMES
            if 0 < len(snapshot.candles.get(timeframe, ())) < PRICE_ACTION_MINIMUM_CANDLES
        )
        items = [
            self._quality(snapshot, under_warmed_timeframes),
            self._ticker(snapshot),
        ]
        tools: list[ToolAssessment] = []

        price_action_ids: list[str] = []
        for timeframe in self._TIMEFRAMES:
            candles = snapshot.candles.get(timeframe)
            if candles and len(candles) >= PRICE_ACTION_MINIMUM_CANDLES:
                item = self._price_action(snapshot.symbol, timeframe, candles)
                items.append(item)
                price_action_ids.append(item.evidence_id)
        raw_sequences = self._raw_sequences(snapshot)
        items.extend(raw_sequences)
        price_action_ids.extend(item.evidence_id for item in raw_sequences)
        core_timeframes: tuple[DeepTimeframe, ...] = (
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "4h",
        )
        core_status = (
            ToolStatus.AVAILABLE
            if all(
                len(snapshot.candles.get(timeframe, ())) >= PRICE_ACTION_MINIMUM_CANDLES
                for timeframe in core_timeframes
            )
            else ToolStatus.PARTIAL
        )
        core_ids = (
            f"{snapshot.symbol}.NATIVE.QUALITY",
            f"{snapshot.symbol}.BYBIT.TICKER",
            *price_action_ids,
        )
        qualified_timeframes = sum(
            len(snapshot.candles.get(timeframe, ())) >= PRICE_ACTION_MINIMUM_CANDLES
            for timeframe in self._TIMEFRAMES
        )
        tools.append(
            ToolAssessment(
                tool="BYBIT_NATIVE_MARKET_DATA",
                status=core_status,
                version="native-v1",
                reason=(
                    "self-contained public REST collector; last and mark remain separate; "
                    f"{qualified_timeframes}/{len(self._TIMEFRAMES)} "
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
                    "REST orderbook is point-in-time advisory evidence; public-trade sums "
                    "describe received samples, not guaranteed continuous order flow"
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
        context_items = self._market_context(snapshot)
        items.extend(context_items)
        tools.append(
            ToolAssessment(
                tool="MARKET_WIDE_CONTEXT",
                status=ToolStatus.AVAILABLE if context_items else ToolStatus.UNAVAILABLE,
                version="native-v2",
                reason="BTC/ETH and Bybit-wide breadth are bounded public context",
                evidence_ids=tuple(item.evidence_id for item in context_items),
            )
        )
        candidate_context = None
        if candidate is not None:
            candidate_context = CandidateContext(
                rank=candidate.rank,
                opportunity_score=candidate.opportunity_score,
                tradability_score=candidate.tradability_score,
                final_score=candidate.score,
                risk_tags=tuple(CandidateRiskTag(tag.value) for tag in candidate.risk_tags),
                raw_features=candidate.features.model_dump(),
            )
            candidate_item = EvidenceItem(
                evidence_id=f"{snapshot.symbol}.FILTER.CONTEXT",
                category="candidate_filter",
                source="NATIVE_TWO_SCORE_FILTER",
                observed_at=candidate.observed_at,
                summary="Filter rank is opportunity/tradability context, never trade direction",
                values=candidate_context.model_dump(mode="json"),
            )
            items.append(candidate_item)
            tools.append(
                ToolAssessment(
                    tool="NATIVE_CANDIDATE_FILTER",
                    status=ToolStatus.AVAILABLE,
                    version="two-score-v3-reviewability",
                    reason="opportunity and tradability are preserved independently",
                    evidence_ids=(candidate_item.evidence_id,),
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
            market_context=snapshot.market_context,
            candidate_context=candidate_context,
        )

    @staticmethod
    def _quality(
        snapshot: NativeMarketSnapshot,
        under_warmed_timeframes: tuple[DeepTimeframe, ...] = (),
    ) -> EvidenceItem:
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
                "qualified_timeframe_count": sum(
                    len(snapshot.candles.get(tf, ())) >= PRICE_ACTION_MINIMUM_CANDLES
                    for tf in EvidenceBuilder._TIMEFRAMES
                ),
                "under_warmed_timeframe_count": len(under_warmed_timeframes),
                "under_warmed_timeframes": (
                    ",".join(under_warmed_timeframes) if under_warmed_timeframes else "none"
                ),
                "collection_duration_ms": round(
                    (snapshot.generated_at - snapshot.collection_started_at).total_seconds() * 1000
                ),
                "evidence_cutoff_utc": snapshot.generated_at.isoformat(),
                "missing_source_count": len(missing),
                "missing_sources": ",".join(missing) if missing else "none",
                "orderbook_available": snapshot.orderbook is not None,
                "public_trade_count": len(snapshot.recent_trades),
                "open_interest_points": len(snapshot.open_interest),
                "long_short_ratio_points": len(snapshot.long_short_ratios),
                "liquidation_status": (
                    snapshot.liquidation_window.status.value
                    if snapshot.liquidation_window is not None
                    else ToolStatus.UNAVAILABLE.value
                ),
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
        true_ranges = _true_ranges(highs, lows, closes)
        ema9_series = _ema_series(closes, 9)
        ema20_series = _ema_series(closes, 20)
        ema50_series = _ema_series(closes, 50)
        ema12_series = _ema_series(closes, 12)
        ema26_series = _ema_series(closes, 26)
        macd_series = [fast - slow for fast, slow in zip(ema12_series, ema26_series, strict=True)]
        signal_series = _ema_series(macd_series, 9)
        histogram_series = [
            macd - signal for macd, signal in zip(macd_series, signal_series, strict=True)
        ]
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
        latest_range = highs[-1] - lows[-1]
        latest_body = closes[-1] - opens[-1]
        latest_upper_wick = highs[-1] - max(opens[-1], closes[-1])
        latest_lower_wick = min(opens[-1], closes[-1]) - lows[-1]
        prior_range_baseline = statistics.median(true_ranges[-12:-3])
        prior_turnover_median = statistics.median(turnovers[-21:-1])
        breakout_reference_high = max(highs[-23:-3])
        breakout_reference_low = min(lows[-23:-3])
        current_rsi = _rsi(closes, 14)
        prior_rsi = _rsi(closes[:-3], 14)
        structure_window = min(8, len(values))
        structure_highs = highs[-structure_window:]
        structure_lows = lows[-structure_window:]
        structure_closes = closes[-structure_window:]
        structure_pairs = tuple(
            zip(
                structure_highs[:-1],
                structure_highs[1:],
                structure_lows[:-1],
                structure_lows[1:],
                structure_closes[:-1],
                structure_closes[1:],
                strict=True,
            )
        )
        rolling_range = rolling_high - rolling_low
        evidence_values: dict[str, str | int | float | bool | None] = {
            "completed_candles": len(values),
            "latest_completed_close_time": values[-1].close_time.isoformat(),
            "latest_close": closes[-1],
            "return_1_percent": _return_percent(closes[-2], closes[-1]),
            "return_3_percent": _return_percent(closes[-4], closes[-1]),
            "return_12_percent": _return_percent(closes[-13], closes[-1]),
            "atr_14": atr14,
            "atr_14_percent": atr14 / closes[-1] * 100,
            "ema_9": ema9_series[-1],
            "ema_20": ema20_series[-1],
            "ema_50": ema50_series[-1],
            "rsi_14": current_rsi,
            "macd_histogram_12_26_9": histogram_series[-1],
            "return_3_acceleration_percent": (
                _return_percent(closes[-4], closes[-1]) - _return_percent(closes[-7], closes[-4])
            ),
            "ema_9_slope_3_atr": _safe_ratio(ema9_series[-1] - ema9_series[-4], atr14),
            "rsi_14_delta_3": current_rsi - prior_rsi,
            "macd_histogram_delta_3_atr": _safe_ratio(
                histogram_series[-1] - histogram_series[-4], atr14
            ),
            "latest_true_range_atr": _safe_ratio(true_ranges[-1], atr14),
            "range_expansion_3_vs_prior_9": (
                statistics.fmean(true_ranges[-3:]) / prior_range_baseline
                if prior_range_baseline > 0
                else None
            ),
            "latest_signed_body_range_ratio": latest_body / latest_range if latest_range else 0.0,
            "latest_close_location": (
                (closes[-1] - lows[-1]) / latest_range if latest_range else 0.5
            ),
            "latest_upper_wick_ratio": latest_upper_wick / latest_range if latest_range else 0.0,
            "latest_lower_wick_ratio": latest_lower_wick / latest_range if latest_range else 0.0,
            "signed_consecutive_close_streak": _signed_close_streak(closes),
            "turnover_impulse_latest_vs_median_20": (
                turnovers[-1] / prior_turnover_median if prior_turnover_median > 0 else None
            ),
            "breakout_reference_high_20_ex_last3": breakout_reference_high,
            "breakout_reference_low_20_ex_last3": breakout_reference_low,
            "last3_closes_above_reference_high": sum(
                close > breakout_reference_high for close in closes[-3:]
            ),
            "last3_closes_below_reference_low": sum(
                close < breakout_reference_low for close in closes[-3:]
            ),
            "rolling_high_20": rolling_high,
            "rolling_low_20": rolling_low,
            "range_mid_20": (rolling_high + rolling_low) / 2,
            "rolling_range_position_20": (
                (closes[-1] - rolling_low) / rolling_range if rolling_range > 0 else 0.5
            ),
            "drawdown_from_rolling_high_atr": _non_negative_ratio(rolling_high - closes[-1], atr14),
            "rebound_from_rolling_low_atr": _non_negative_ratio(closes[-1] - rolling_low, atr14),
            "higher_high_steps_8": sum(
                current_high > prior_high
                for prior_high, current_high, _, _, _, _ in structure_pairs
            ),
            "higher_low_steps_8": sum(
                current_low > prior_low for _, _, prior_low, current_low, _, _ in structure_pairs
            ),
            "lower_high_steps_8": sum(
                current_high < prior_high
                for prior_high, current_high, _, _, _, _ in structure_pairs
            ),
            "lower_low_steps_8": sum(
                current_low < prior_low for _, _, prior_low, current_low, _, _ in structure_pairs
            ),
            "close_up_steps_8": sum(
                current_close > prior_close
                for _, _, _, _, prior_close, current_close in structure_pairs
            ),
            "close_down_steps_8": sum(
                current_close < prior_close
                for _, _, _, _, prior_close, current_close in structure_pairs
            ),
            "recent_two_bull_count": sum(
                close > open_ for close, open_ in zip(closes[-2:], opens[-2:], strict=True)
            ),
            "recent_two_bear_count": sum(
                close < open_ for close, open_ in zip(closes[-2:], opens[-2:], strict=True)
            ),
            "recent_two_turnover_vs_median_20": (
                statistics.fmean(turnovers[-2:]) / prior_turnover_median
                if prior_turnover_median > 0
                else None
            ),
            "directional_efficiency_12": _directional_efficiency(closes[-lookback:]),
            "absolute_directional_efficiency_12": abs(_directional_efficiency(closes[-lookback:])),
            "overlap_ratio_12": _overlap_ratio(highs[-lookback:], lows[-lookback:]),
            "bull_body_ratio_12": sum(
                close > open_
                for close, open_ in zip(closes[-lookback:], opens[-lookback:], strict=True)
            )
            / lookback,
            "turnover_ratio_6_vs_6": (
                recent_turnover / prior_turnover if prior_turnover > 0 else None
            ),
            "latest_turnover": turnovers[-1],
            "recent_6_bars_turnover_usdt": recent_turnover,
            "recent_turnover_window_minutes": (
                candles[-1].close_time - candles[-6].open_time
            ).total_seconds()
            / 60,
            "confirmed_pivot_high": pivot_high,
            "pivot_high_open_time": pivot_high_at,
            "pivot_high_confirmed_at": pivot_high_confirmed,
            "pivot_high_age_bars": pivot_high_age,
            "confirmed_pivot_low": pivot_low,
            "pivot_low_open_time": pivot_low_at,
            "pivot_low_confirmed_at": pivot_low_confirmed,
            "pivot_low_age_bars": pivot_low_age,
            "distance_below_pivot_high_atr": (
                _safe_ratio(pivot_high - closes[-1], atr14) if pivot_high is not None else None
            ),
            "distance_above_pivot_low_atr": (
                _safe_ratio(closes[-1] - pivot_low, atr14) if pivot_low is not None else None
            ),
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

    @staticmethod
    def _raw_sequences(snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        limits: dict[DeepTimeframe, int] = {
            "3m": 24,
            "5m": 24,
            "15m": 16,
            "30m": 12,
        }
        items: list[EvidenceItem] = []
        for timeframe, limit in limits.items():
            candles = snapshot.candles.get(timeframe)
            if not candles:
                continue
            values = candles[-limit:]
            items.append(
                EvidenceItem(
                    evidence_id=f"{snapshot.symbol}.RAW.{timeframe.upper()}",
                    category="compact_raw_candles",
                    source="BYBIT_NATIVE_COMPLETED_CANDLES",
                    observed_at=values[-1].close_time,
                    summary=f"Latest {len(values)} completed {timeframe} OHLCV rows",
                    values={
                        "timeframe": timeframe,
                        "columns": [
                            "open_time",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "turnover",
                        ],
                        "rows": [
                            [
                                candle.open_time.isoformat(),
                                float(candle.open),
                                float(candle.high),
                                float(candle.low),
                                float(candle.close),
                                float(candle.volume),
                                float(candle.turnover),
                            ]
                            for candle in values
                        ],
                    },
                )
            )
        return items

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
            for seconds, label in ((30, "30S"), (60, "1M"), (300, "5M")):
                items.append(self._trade_window(snapshot, seconds=seconds, label=label))
        return items

    @staticmethod
    def _trade_window(
        snapshot: NativeMarketSnapshot,
        *,
        seconds: int,
        label: str,
    ) -> EvidenceItem:
        cutoff = snapshot.generated_at - timedelta(seconds=seconds)
        trades = [trade for trade in snapshot.recent_trades if trade.timestamp >= cutoff]
        oldest = snapshot.recent_trades[0].timestamp
        newest = snapshot.recent_trades[-1].timestamp
        qualified = oldest <= cutoff and newest >= snapshot.generated_at - timedelta(seconds=30)
        buy = sum(float(trade.notional) for trade in trades if trade.side == "Buy")
        sell = sum(float(trade.notional) for trade in trades if trade.side == "Sell")
        total = buy + sell
        return EvidenceItem(
            evidence_id=f"{snapshot.symbol}.MICRO.TRADES.{label}",
            category="order_flow",
            source="BYBIT_PUBLIC_RECENT_TRADES",
            observed_at=newest,
            summary=(
                f"{label.lower()} REST trade sample spans lookback; continuous coverage unverified"
                if qualified
                else f"{label.lower()} trade window coverage is incomplete; delta excluded"
            ),
            values={
                "qualified": qualified,
                "coverage_complete": False,
                "coverage_method": "REST sample endpoint-span only; not a continuous trade stream",
                "latest_sample_age_seconds": (snapshot.generated_at - newest).total_seconds(),
                "sample_count": len(trades),
                "oldest_trade_time": oldest.isoformat(),
                "newest_trade_time": newest.isoformat(),
                "buy_notional": buy if qualified else None,
                "sell_notional": sell if qualified else None,
                "signed_delta_notional": buy - sell if qualified else None,
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
            and not snapshot.long_short_ratios
        ):
            return EvidenceBuilder._liquidations(snapshot)
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
                        float(ticker.open_interest) if ticker.open_interest is not None else None
                    ),
                    "current_open_interest_value": (
                        float(ticker.open_interest_value)
                        if ticker.open_interest_value is not None
                        else None
                    ),
                    "oi_history_points": len(points),
                    "oi_change_15m_percent": _oi_change(points, 3, snapshot.generated_at),
                    "oi_change_1h_percent": _oi_change(points, 12, snapshot.generated_at),
                    "next_funding_time": (
                        ticker.next_funding_time.isoformat()
                        if ticker.next_funding_time is not None
                        else None
                    ),
                    "account_ratio_points": len(snapshot.long_short_ratios),
                    "latest_buy_ratio": (
                        float(snapshot.long_short_ratios[-1].buy_ratio)
                        if snapshot.long_short_ratios
                        else None
                    ),
                    "latest_sell_ratio": (
                        float(snapshot.long_short_ratios[-1].sell_ratio)
                        if snapshot.long_short_ratios
                        else None
                    ),
                    "latest_long_short_ratio": (
                        float(value)
                        if snapshot.long_short_ratios
                        and (value := snapshot.long_short_ratios[-1].long_short_ratio) is not None
                        else None
                    ),
                },
            ),
            *EvidenceBuilder._liquidations(snapshot),
        ]

    @staticmethod
    def _liquidations(snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        for label, window in (("5M", snapshot.liquidation_window),):
            if window is None:
                continue
            items.append(
                EvidenceItem(
                    evidence_id=f"{snapshot.symbol}.LIQUIDATIONS.{label}",
                    category="liquidations",
                    source="BYBIT_PUBLIC_ALL_LIQUIDATION_STREAM",
                    observed_at=window.generated_at,
                    summary=(
                        "Liquidation zero is valid only when stream coverage is complete; "
                        f"current status is {window.status.value}"
                    ),
                    values=window.model_dump(mode="json"),
                )
            )
        return items

    @staticmethod
    def _market_context(snapshot: NativeMarketSnapshot) -> list[EvidenceItem]:
        context = snapshot.market_context
        if context is None:
            return []
        return [
            EvidenceItem(
                evidence_id=f"{snapshot.symbol}.MARKET.CONTEXT",
                category="market_context",
                source="BYBIT_NATIVE_MARKET_CONTEXT",
                observed_at=context.generated_at,
                summary="BTC/ETH short-term context and Bybit linear-perpetual breadth",
                values=context.model_dump(mode="json"),
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


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _non_negative_ratio(numerator: float, denominator: float) -> float | None:
    ratio = _safe_ratio(numerator, denominator)
    return max(0.0, ratio) if ratio is not None else None


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
    true_ranges = _true_ranges(highs, lows, closes)
    atr = statistics.fmean(true_ranges[1 : period + 1])
    for value in true_ranges[period + 1 :]:
        atr = (atr * (period - 1) + value) / period
    return atr


def _true_ranges(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> list[float]:
    values = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        values.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return values


def _rsi(closes: Sequence[float], period: int) -> float:
    changes = [current - previous for previous, current in pairwise(closes)]
    if len(changes) < period:
        raise ValueError("RSI window is too short")
    gain = statistics.fmean(max(change, 0) for change in changes[:period])
    loss = statistics.fmean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _return_percent(start: float, end: float) -> float:
    return (end / start - 1) * 100


def _signed_close_streak(closes: Sequence[float]) -> int:
    direction = 0
    count = 0
    for previous, current in reversed(tuple(pairwise(closes))):
        current_direction = 1 if current > previous else -1 if current < previous else 0
        if current_direction == 0:
            break
        if direction == 0:
            direction = current_direction
        if current_direction != direction:
            break
        count += 1
    return direction * count


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
        neighbors = [getattr(candles[position], field) for position in range(index - 2, index + 3)]
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


def _oi_change(points: Sequence[BybitOpenInterest], periods: int, as_of=None) -> float | None:
    if len(points) <= periods:
        return None
    window = points[-periods - 1 :]
    if any(b.timestamp - a.timestamp != timedelta(minutes=5) for a, b in pairwise(window)):
        return None
    if as_of is not None and not timedelta(0) <= as_of - window[-1].timestamp <= timedelta(
        minutes=10
    ):
        return None
    current = float(window[-1].open_interest)
    previous = float(window[0].open_interest)
    return (current / previous - 1) * 100 if previous > 0 else None
