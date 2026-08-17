from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from bybit_signal.cmi.models import CmiSnapshot
from bybit_signal.domain.enums import PriceType, ToolStatus
from bybit_signal.domain.models import (
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    ToolAssessment,
)


class EvidenceBuilder:
    """Extract neutral, auditable facts without deciding a trade direction."""

    _TIMEFRAMES = ("5m", "15m", "1h", "4h")

    def build(self, snapshot: CmiSnapshot) -> EvidenceBundle:
        payload = snapshot.payload
        ticker = self._mapping(self._mapping(payload.get("perpetual_tickers")).get("bybit"))
        last = self._canonical_price(snapshot.symbol, ticker, PriceType.LAST)
        mark = self._canonical_price(snapshot.symbol, ticker, PriceType.MARK)
        items: list[EvidenceItem] = []
        tools: list[ToolAssessment] = []

        health_id = f"{snapshot.symbol}.CMI.HEALTH"
        price_id = f"{snapshot.symbol}.CMI.PRICE"
        items.extend(
            (
                self._health_evidence(snapshot, health_id),
                self._price_evidence(snapshot, ticker, price_id),
            )
        )
        tools.append(
            ToolAssessment(
                tool="CMI",
                status=snapshot.status,
                version=snapshot.application_version,
                reason=(
                    f"validated schema {snapshot.schema_version}; "
                    f"{len(snapshot.available_perpetual_exchanges)} perpetual source(s); "
                    f"{snapshot.limitation_count} declared limitation(s)"
                ),
                evidence_ids=(health_id, price_id),
            )
        )

        price_action_ids: list[str] = []
        for timeframe in self._TIMEFRAMES:
            evidence = self._price_action(snapshot, timeframe)
            if evidence is not None:
                items.append(evidence)
                price_action_ids.append(evidence.evidence_id)
        tools.append(
            ToolAssessment(
                tool="LOUIE_NATIVE_PRICE_ACTION",
                status=ToolStatus.AVAILABLE
                if len(price_action_ids) == len(self._TIMEFRAMES)
                else ToolStatus.PARTIAL,
                version="native-v1",
                reason=(
                    "completed-candle-only multi-timeframe structure; "
                    "confirmed pivots use a right-side "
                    "confirmation window and never inspect forming candles"
                ),
                evidence_ids=tuple(price_action_ids),
            )
        )

        order_flow_items, order_flow_status, order_flow_reason = self._order_flow(snapshot)
        items.extend(order_flow_items)
        tools.append(
            ToolAssessment(
                tool="PYTA_NATIVE_ORDER_FLOW",
                status=order_flow_status,
                version="native-v1",
                reason=order_flow_reason,
                evidence_ids=tuple(item.evidence_id for item in order_flow_items),
            )
        )

        derivatives = self._derivatives(snapshot)
        items.extend(derivatives)
        tools.append(
            ToolAssessment(
                tool="CMI_DERIVATIVES",
                status=ToolStatus.AVAILABLE if derivatives else ToolStatus.UNAVAILABLE,
                version=snapshot.application_version,
                reason=(
                    "funding, open interest and liquidation windows "
                    "retain their source quality flags"
                ),
                evidence_ids=tuple(item.evidence_id for item in derivatives),
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

    def _health_evidence(self, snapshot: CmiSnapshot, evidence_id: str) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=evidence_id,
            category="data_quality",
            source="CMI",
            observed_at=snapshot.generated_at,
            summary=(
                f"CMI {snapshot.status}: {snapshot.completeness_status}, "
                f"health {snapshot.health_status}; absent fields remain unknown"
            ),
            values={
                "snapshot_status": snapshot.snapshot_status,
                "completeness_status": snapshot.completeness_status,
                "health_status": snapshot.health_status,
                "limitation_count": snapshot.limitation_count,
                "perpetual_exchanges": ",".join(snapshot.available_perpetual_exchanges),
            },
        )

    def _price_evidence(
        self,
        snapshot: CmiSnapshot,
        ticker: Mapping[str, Any],
        evidence_id: str,
    ) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=evidence_id,
            category="canonical_price",
            source="CMI_BYBIT_PERPETUAL",
            observed_at=snapshot.generated_at,
            summary=(
                "Bybit perpetual last and mark are preserved separately; "
                "no exchange averaging is used"
            ),
            values={
                "last_price": self._number(ticker.get("last_price")),
                "mark_price": self._number(ticker.get("mark_price")),
                "index_price": self._number(ticker.get("index_price")),
                "bid_price": self._number(ticker.get("bid_price")),
                "ask_price": self._number(ticker.get("ask_price")),
                "funding_rate": self._number(ticker.get("funding_rate")),
                "turnover_24h": self._number(ticker.get("turnover_24h")),
            },
        )

    def _price_action(self, snapshot: CmiSnapshot, timeframe: str) -> EvidenceItem | None:
        candles_root = self._mapping(snapshot.payload.get("candles"))
        bybit = self._mapping(candles_root.get("bybit"))
        window = self._mapping(bybit.get(timeframe))
        completed_value = window.get("completed")
        if not isinstance(completed_value, list):
            return None
        candles = [
            cast(Mapping[str, Any], candle)
            for candle in completed_value
            if isinstance(candle, Mapping)
            and candle.get("complete") is True
            and candle.get("normalized_symbol") == snapshot.symbol
            and candle.get("interval") == timeframe
            and candle.get("gap_detected") is not True
            and candle.get("is_stale") is not True
        ]
        candles.sort(key=lambda candle: int(self._number(candle.get("open_time_ms")) or 0))
        if len(candles) < 20:
            return None
        recent = candles[-60:]
        closes = [self._required_number(candle.get("close")) for candle in recent]
        highs = [self._required_number(candle.get("high")) for candle in recent]
        lows = [self._required_number(candle.get("low")) for candle in recent]
        opens = [self._required_number(candle.get("open")) for candle in recent]
        ranges = [max(high - low, 0.0) for high, low in zip(highs, lows, strict=True)]
        atr14 = sum(ranges[-14:]) / 14
        rolling_high = max(highs[-20:])
        rolling_low = min(lows[-20:])
        net_move = closes[-1] - closes[-13]
        travelled = sum(
            abs(closes[index] - closes[index - 1]) for index in range(len(closes) - 11, len(closes))
        )
        efficiency = net_move / travelled if travelled else 0.0
        overlap = self._overlap_ratio(highs[-12:], lows[-12:])
        swing_high, swing_high_confirmed = self._last_confirmed_pivot(recent, "high")
        swing_low, swing_low_confirmed = self._last_confirmed_pivot(recent, "low")
        indicators = self._mapping(
            self._mapping(
                self._mapping(snapshot.payload.get("technical_indicators")).get("bybit")
            ).get("perp")
        )
        technical = self._mapping(indicators.get(timeframe))
        values: dict[str, str | int | float | bool | None] = {
            "completed_candles": len(candles),
            "latest_completed_open_time_ms": int(
                self._required_number(recent[-1].get("open_time_ms"))
            ),
            "latest_close": closes[-1],
            "return_3_percent": self._return_percent(closes[-4], closes[-1]),
            "return_12_percent": self._return_percent(closes[-13], closes[-1]),
            "atr14": atr14,
            "atr14_percent": atr14 / closes[-1] * 100,
            "rolling_high_20": rolling_high,
            "rolling_low_20": rolling_low,
            "range_mid_20": (rolling_high + rolling_low) / 2,
            "directional_efficiency_12": efficiency,
            "overlap_ratio_12": overlap,
            "bull_body_ratio_12": sum(
                close > open_ for close, open_ in zip(closes[-12:], opens[-12:], strict=True)
            )
            / 12,
            "confirmed_swing_high": swing_high,
            "swing_high_confirmed_at_ms": swing_high_confirmed,
            "confirmed_swing_low": swing_low,
            "swing_low_confirmed_at_ms": swing_low_confirmed,
            "ema_9": self._indicator_value(technical, "ema_9"),
            "ema_20": self._indicator_value(technical, "ema_20"),
            "ema_50": self._indicator_value(technical, "ema_50"),
            "rsi_14": self._indicator_value(technical, "rsi_14"),
            "macd_histogram": self._indicator_value(technical, "macd_histogram"),
        }
        return EvidenceItem(
            evidence_id=f"{snapshot.symbol}.PA.{timeframe.upper()}",
            category="price_action",
            source="CMI_BYBIT_COMPLETED_CANDLES",
            observed_at=snapshot.generated_at,
            summary=(
                f"{timeframe} completed-candle structure: close {closes[-1]:g}, "
                f"20-bar range {rolling_low:g}-{rolling_high:g}; forming candle excluded"
            ),
            values=values,
        )

    def _order_flow(self, snapshot: CmiSnapshot) -> tuple[list[EvidenceItem], ToolStatus, str]:
        items: list[EvidenceItem] = []
        qualified_count = 0
        order_flow = self._mapping(snapshot.payload.get("order_flow"))
        bybit = self._mapping(self._mapping(order_flow.get("bybit")).get("perp"))
        for window_name in ("1m", "5m", "15m"):
            window = self._mapping(bybit.get(window_name))
            qualified = self._flow_qualified(window)
            values: dict[str, str | int | float | bool | None] = {
                "qualified": qualified,
                "quality_status": str(window.get("quality_status") or "UNKNOWN"),
                "coverage_ratio": self._number(window.get("coverage_ratio")),
                "sample_count": int(self._number(window.get("sample_count")) or 0),
                "stream_gap_detected": window.get("stream_gap_detected") is True,
                "truncated_by_api_limit": window.get("truncated_by_api_limit") is True,
            }
            if qualified:
                buy = self._required_number(window.get("window_buy_notional"))
                sell = self._required_number(window.get("window_sell_notional"))
                total = buy + sell
                values.update(
                    {
                        "window_delta": self._number(window.get("window_delta")),
                        "normalized_delta": (buy - sell) / total if total else None,
                        "buy_sell_ratio": self._number(window.get("buy_sell_ratio")),
                        "large_trade_count": int(
                            self._number(window.get("large_trade_count")) or 0
                        ),
                    }
                )
                qualified_count += 1
            items.append(
                EvidenceItem(
                    evidence_id=f"{snapshot.symbol}.OF.{window_name.upper()}",
                    category="order_flow",
                    source="CMI_BYBIT_PUBLIC_TRADES",
                    observed_at=snapshot.generated_at,
                    summary=(
                        f"{window_name} order flow is qualified"
                        if qualified
                        else (
                            f"{window_name} order flow is excluded from directional use "
                            "because coverage is incomplete"
                        )
                    ),
                    values=values,
                )
            )

        book = self._mapping(self._mapping(snapshot.payload.get("orderbook")).get("bybit:perp"))
        book_qualified = all(
            (
                book.get("available") is True,
                book.get("partial") is not True,
                book.get("quality_status") == "QUALIFIED",
                book.get("sequence_healthy") is True,
                book.get("snapshot_healthy") is True,
                book.get("stream_healthy") is True,
            )
        )
        book_values: dict[str, str | int | float | bool | None] = {
            "qualified": book_qualified,
            "spread_bps": self._number(book.get("spread_bps")),
            "resync_count": int(self._number(book.get("resync_count")) or 0),
            "sequence_gap_count": int(self._number(book.get("sequence_gap_count")) or 0),
        }
        if book_qualified:
            level_imbalance = self._mapping(book.get("level_imbalance"))
            depth = self._mapping(book.get("depth"))
            book_values.update(
                {
                    "imbalance_top5": self._number(level_imbalance.get("top5")),
                    "imbalance_top20": self._number(level_imbalance.get("top20")),
                    "depth_10bps_bid_notional": self._nested_number(depth, "10bps", "bid_notional"),
                    "depth_10bps_ask_notional": self._nested_number(depth, "10bps", "ask_notional"),
                }
            )
        items.append(
            EvidenceItem(
                evidence_id=f"{snapshot.symbol}.OF.BOOK",
                category="orderbook",
                source="CMI_BYBIT_L2",
                observed_at=snapshot.generated_at,
                summary=(
                    "Bybit L2 book is sequence/snapshot/stream qualified"
                    if book_qualified
                    else (
                        "Bybit L2 book is fail-closed because its reconstruction "
                        "health is incomplete"
                    )
                ),
                values=book_values,
            )
        )
        if book_qualified:
            qualified_count += 1
        status = ToolStatus.AVAILABLE if qualified_count == 4 else ToolStatus.PARTIAL
        reason = (
            f"{qualified_count}/4 order-flow components qualified; aggregate data does not support "
            "inventing order identity, sweep or absorption labels"
        )
        return items, status, reason

    def _derivatives(self, snapshot: CmiSnapshot) -> list[EvidenceItem]:
        ticker = self._mapping(
            self._mapping(snapshot.payload.get("perpetual_tickers")).get("bybit")
        )
        values: dict[str, str | int | float | bool | None] = {
            "funding_rate": self._number(ticker.get("funding_rate")),
            "open_interest_value": self._number(ticker.get("open_interest_value")),
        }
        open_interest = self._mapping(
            self._mapping(snapshot.payload.get("open_interest")).get("bybit")
        )
        for window_name in ("15m", "1h", "4h"):
            window = self._mapping(open_interest.get(window_name))
            qualified = (
                window.get("available") is True
                and window.get("coverage_complete") is True
                and window.get("quality_status") == "QUALIFIED"
            )
            values[f"oi_{window_name}_qualified"] = qualified
            if qualified:
                values[f"oi_{window_name}_change_percent"] = self._number(
                    window.get("percent_change")
                )

        liquidations = self._mapping(snapshot.payload.get("liquidations"))
        liquidation_5m = self._mapping(liquidations.get("5m"))
        liquidation_qualified = (
            liquidation_5m.get("available") is True
            and liquidation_5m.get("coverage_complete") is True
            and liquidation_5m.get("partial") is not True
        )
        values["liquidations_5m_qualified"] = liquidation_qualified
        if liquidation_qualified:
            values["long_liquidation_notional_5m"] = self._number(
                liquidation_5m.get("long_liquidation_notional")
            )
            values["short_liquidation_notional_5m"] = self._number(
                liquidation_5m.get("short_liquidation_notional")
            )
        return [
            EvidenceItem(
                evidence_id=f"{snapshot.symbol}.DERIVATIVES",
                category="derivatives",
                source="CMI_PUBLIC_DERIVATIVES",
                observed_at=snapshot.generated_at,
                summary=(
                    "Funding and quality-qualified OI/liquidation windows; "
                    "partial windows expose no directional value"
                ),
                values=values,
            )
        ]

    @staticmethod
    def _canonical_price(
        symbol: str,
        ticker: Mapping[str, Any],
        price_type: PriceType,
    ) -> CanonicalPrice:
        field = "last_price" if price_type is PriceType.LAST else "mark_price"
        time_field = "last_price_time_ms" if price_type is PriceType.LAST else "mark_price_time_ms"
        timestamp_ms = EvidenceBuilder._required_number(ticker.get(time_field))
        return CanonicalPrice(
            symbol=symbol,
            price_type=price_type,
            value=Decimal(str(EvidenceBuilder._required_number(ticker.get(field)))),
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
        )

    @staticmethod
    def _flow_qualified(window: Mapping[str, Any]) -> bool:
        return all(
            (
                window.get("available") is True,
                window.get("coverage_complete") is True,
                window.get("partial") is not True,
                window.get("quality_status") == "QUALIFIED",
                window.get("status") == "COMPLETE",
                window.get("stream_gap_detected") is not True,
                window.get("truncated_by_api_limit") is not True,
                (EvidenceBuilder._number(window.get("sample_count")) or 0) > 0,
            )
        )

    @staticmethod
    def _last_confirmed_pivot(
        candles: list[Mapping[str, Any]], side: str
    ) -> tuple[float | None, int | None]:
        field = "high" if side == "high" else "low"
        values = [EvidenceBuilder._required_number(candle.get(field)) for candle in candles]
        comparison = max if side == "high" else min
        found: tuple[float | None, int | None] = (None, None)
        for index in range(2, len(values) - 2):
            window = values[index - 2 : index + 3]
            if values[index] == comparison(window) and window.count(values[index]) == 1:
                confirmed_at = int(
                    EvidenceBuilder._required_number(candles[index + 2].get("open_time_ms"))
                )
                found = values[index], confirmed_at
        return found

    @staticmethod
    def _overlap_ratio(highs: list[float], lows: list[float]) -> float:
        overlaps: list[float] = []
        for index in range(1, len(highs)):
            union = max(highs[index], highs[index - 1]) - min(lows[index], lows[index - 1])
            overlap = max(
                0.0, min(highs[index], highs[index - 1]) - max(lows[index], lows[index - 1])
            )
            overlaps.append(overlap / union if union else 0.0)
        return sum(overlaps) / len(overlaps) if overlaps else 0.0

    @staticmethod
    def _indicator_value(technical: Mapping[str, Any], name: str) -> float | None:
        indicator = EvidenceBuilder._mapping(technical.get(name))
        if indicator.get("available") is not True:
            return None
        return EvidenceBuilder._number(indicator.get("value"))

    @staticmethod
    def _nested_number(value: Mapping[str, Any], first: str, second: str) -> float | None:
        return EvidenceBuilder._number(EvidenceBuilder._mapping(value.get(first)).get(second))

    @staticmethod
    def _return_percent(start: float, end: float) -> float:
        return (end / start - 1) * 100 if start else 0.0

    @staticmethod
    def _mapping(value: object) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @staticmethod
    def _required_number(value: object) -> float:
        result = EvidenceBuilder._number(value)
        if result is None:
            raise ValueError("required numeric market field is missing")
        return result
