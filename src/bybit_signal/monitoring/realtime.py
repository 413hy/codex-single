from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import websockets

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import MonitoringMetric
from bybit_signal.monitoring.engine import ThresholdEngine, ThresholdEvent, WakeLimiter
from bybit_signal.providers.public_streams import PublicLiquidation, PublicStreamCache
from bybit_signal.storage.sqlite import SignalStore

EmergencyCallback = Callable[[str, tuple[ThresholdEvent, ...]], Awaitable[None]]
TriggerCallback = Callable[[tuple[ThresholdEvent, ...]], Awaitable[None]]
logger = logging.getLogger(__name__)
_SCHEDULED_REFRESH_PAUSE_KEY = "monitoring.scheduled_refresh_paused_at"


class RealtimeMonitor:
    def __init__(
        self,
        *,
        websocket_url: str,
        config: MonitoringConfig,
        store: SignalStore,
        on_emergency: EmergencyCallback,
        on_trigger: TriggerCallback | None = None,
        stream_cache: PublicStreamCache | None = None,
    ) -> None:
        self._websocket_url = websocket_url
        self._config = config
        self._store = store
        self._on_emergency = on_emergency
        self._on_trigger = on_trigger
        self._stream_cache = stream_cache
        self._engine = ThresholdEngine()
        self._limiter = WakeLimiter(config)
        self._pending: dict[str, list[ThresholdEvent]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._trades: dict[str, deque[tuple[datetime, Decimal]]] = {}
        self._liquidations: dict[str, deque[tuple[datetime, Decimal]]] = {}
        self._trade_coverage_start: dict[str, datetime] = {}
        self._liquidation_coverage_start: dict[str, datetime] = {}
        self._orderbooks: dict[
            str, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]
        ] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        await self._refresh_directives()
        while not stop_event.is_set():
            topics = self._topics()
            if not topics:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._config.directive_refresh_seconds,
                    )
                except TimeoutError:
                    await self._refresh_directives()
                continue
            try:
                async with websockets.connect(
                    self._websocket_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                    close_timeout=5,
                    max_size=2 * 1024 * 1024,
                ) as websocket:
                    # A reconnect starts a new exchange book sequence. Ignore deltas until
                    # each topic supplies its fresh snapshot instead of extending stale state.
                    self._orderbooks.clear()
                    self._trades.clear()
                    self._liquidations.clear()
                    self._trade_coverage_start.clear()
                    self._liquidation_coverage_start.clear()
                    for index in range(0, len(topics), 10):
                        await websocket.send(
                            json.dumps({"op": "subscribe", "args": topics[index : index + 10]})
                        )
                    if self._stream_cache is not None:
                        for symbol in self._symbols():
                            self._stream_cache.mark_subscribed(symbol)
                    loop = asyncio.get_running_loop()
                    refresh_deadline = loop.time() + self._config.directive_refresh_seconds
                    while not stop_event.is_set():
                        message: str | bytes | None = None
                        with suppress(TimeoutError):
                            message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=max(0.001, refresh_deadline - loop.time()),
                            )
                        if loop.time() >= refresh_deadline:
                            previous_topics = topics
                            await self._refresh_directives()
                            topics = self._topics()
                            refresh_deadline = loop.time() + self._config.directive_refresh_seconds
                            if topics != previous_topics:
                                break
                        if message is None:
                            continue
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        await self._handle_message(message)
            except (OSError, websockets.WebSocketException, ValueError) as error:
                logger.warning("public WebSocket reconnecting after %s", type(error).__name__)
                if self._stream_cache is not None:
                    self._stream_cache.mark_reconnect(self._symbols())
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._config.reconnect_delay_seconds,
                    )
                except TimeoutError:
                    await self._refresh_directives()

    async def close(self) -> None:
        for task in self._flush_tasks.values():
            task.cancel()
        if self._flush_tasks:
            await asyncio.gather(*self._flush_tasks.values(), return_exceptions=True)

    async def pause_for_scheduled_refresh(
        self,
        *,
        paused_at: datetime | None = None,
    ) -> None:
        """Persistently disable all old thresholds before an authoritative refresh."""

        timestamp = paused_at or datetime.now(UTC)
        await self._store.set_runtime_state(
            _SCHEDULED_REFRESH_PAUSE_KEY,
            timestamp.isoformat(),
        )
        self._engine.replace_conclusions(())
        logger.info("realtime thresholds paused for scheduled refresh")

    async def resume_after_scheduled_success(self) -> None:
        """Activate only the newest successful scheduled analysis thresholds."""

        await self._store.delete_runtime_state(_SCHEDULED_REFRESH_PAUSE_KEY)
        await self._refresh_directives()
        logger.info("realtime thresholds resumed after scheduled success")

    async def refresh_now(self) -> None:
        """Reload the newest committed monitoring version without changing pause state."""

        await self._refresh_directives()

    async def wait_until_idle(self) -> None:
        """Wait for threshold reviews accepted before the pause to finish."""

        while self._flush_tasks:
            await asyncio.gather(
                *tuple(self._flush_tasks.values()),
                return_exceptions=True,
            )

    async def _refresh_directives(self) -> None:
        paused_at_value = await self._store.runtime_state(_SCHEDULED_REFRESH_PAUSE_KEY)
        if paused_at_value is not None:
            # A successful direction cycle is not enough to reactivate monitoring.
            # Only the scheduler may clear this durable pause after a usable reviewed
            # monitoring version has committed. This prevents a reconnect/restart from
            # bypassing a failed model review.
            self._engine.replace_conclusions(())
            return
        conclusions = await self._store.active_monitoring_conclusions()
        analysis_ids = tuple(sorted({conclusion.analysis_id for conclusion in conclusions}))
        suspended = await self._store.triggered_analysis_symbol_keys(analysis_ids)
        self._engine.replace_conclusions(conclusions, suspended_keys=suspended)

    def _topics(self) -> list[str]:
        directives = self._engine.directives()
        metric_pairs = tuple(
            (active, metric)
            for active in directives
            for metric in {
                active.directive.metric,
                *(condition.metric for condition in active.directive.confirmations),
            }
        )
        ticker_symbols = {
            active.symbol
            for active, metric in metric_pairs
            if metric
            in {
                MonitoringMetric.LAST_PRICE,
                MonitoringMetric.MARK_PRICE,
                MonitoringMetric.SPREAD_BPS,
                MonitoringMetric.OPEN_INTEREST,
                MonitoringMetric.FUNDING_RATE,
            }
        }
        kline_intervals: dict[MonitoringMetric, str] = {
            MonitoringMetric.COMPLETED_1M_CLOSE: "1",
            MonitoringMetric.TURNOVER_1M: "1",
            MonitoringMetric.COMPLETED_5M_CLOSE: "5",
            MonitoringMetric.COMPLETED_15M_CLOSE: "15",
            MonitoringMetric.COMPLETED_30M_CLOSE: "30",
            MonitoringMetric.COMPLETED_1H_CLOSE: "60",
        }
        topics = [f"tickers.{symbol}" for symbol in sorted(ticker_symbols)]
        topics.extend(
            f"kline.{interval}.{active.symbol}"
            for active, metric in metric_pairs
            if (interval := kline_intervals.get(metric)) is not None
        )
        trade_symbols = {
            active.symbol
            for active, metric in metric_pairs
            if metric is MonitoringMetric.TRADE_DELTA_30S
        }
        book_symbols = {
            active.symbol
            for active, metric in metric_pairs
            if metric is MonitoringMetric.ORDERBOOK_IMBALANCE_L5
        }
        liquidation_symbols = {
            active.symbol
            for active, metric in metric_pairs
            if metric is MonitoringMetric.LIQUIDATION_NOTIONAL_1M
        }
        topics.extend(f"publicTrade.{symbol}" for symbol in sorted(trade_symbols))
        topics.extend(f"orderbook.50.{symbol}" for symbol in sorted(book_symbols))
        topics.extend(f"allLiquidation.{symbol}" for symbol in sorted(liquidation_symbols))
        return sorted(set(topics))

    def _symbols(self) -> tuple[str, ...]:
        return tuple(sorted({active.symbol for active in self._engine.directives()}))

    async def _handle_message(self, message: str) -> None:
        try:
            document = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(document, Mapping):
            return
        topic = document.get("topic")
        data = document.get("data")
        timestamp = _timestamp(document.get("ts"))
        if not isinstance(topic, str) or timestamp is None:
            return
        symbol_for_heartbeat = topic.rsplit(".", 1)[-1]
        if self._stream_cache is not None and symbol_for_heartbeat.endswith("USDT"):
            self._stream_cache.heartbeat(symbol_for_heartbeat, at=timestamp)
        observations: list[tuple[str, MonitoringMetric, Decimal]] = []
        if topic.startswith("tickers.") and isinstance(data, Mapping):
            symbol = topic.split(".", 1)[1]
            ticker = cast(Mapping[str, Any], data)
            observations.extend(_ticker_observations(symbol, ticker))
        elif topic.startswith("kline.") and isinstance(data, list):
            parts = topic.split(".")
            if len(parts) != 3:
                return
            metric = {
                "1": MonitoringMetric.COMPLETED_1M_CLOSE,
                "5": MonitoringMetric.COMPLETED_5M_CLOSE,
                "15": MonitoringMetric.COMPLETED_15M_CLOSE,
                "30": MonitoringMetric.COMPLETED_30M_CLOSE,
                "60": MonitoringMetric.COMPLETED_1H_CLOSE,
            }.get(parts[1])
            if metric is not None:
                for item in data:
                    if isinstance(item, Mapping) and item.get("confirm") is True:
                        close = _decimal(item.get("close"))
                        if close is not None:
                            observations.append((parts[2], metric, close))
                        if parts[1] == "1":
                            turnover = _decimal(item.get("turnover"))
                            if turnover is not None:
                                observations.append(
                                    (parts[2], MonitoringMetric.TURNOVER_1M, turnover)
                                )
        elif topic.startswith("publicTrade.") and isinstance(data, list):
            symbol = topic.split(".", 1)[1]
            coverage_start = self._trade_coverage_start.setdefault(symbol, timestamp)
            values = self._trades.setdefault(symbol, deque())
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                price = _decimal(item.get("p"))
                size = _decimal(item.get("v"))
                side = item.get("S")
                event_at = _timestamp(item.get("T")) or timestamp
                if price is None or size is None or side not in {"Buy", "Sell"}:
                    continue
                signed = price * size if side == "Buy" else -(price * size)
                values.append((event_at, signed))
            cutoff = timestamp.timestamp() - 30
            while values and values[0][0].timestamp() <= cutoff:
                values.popleft()
            if (timestamp - coverage_start).total_seconds() >= 30:
                observations.append(
                    (
                        symbol,
                        MonitoringMetric.TRADE_DELTA_30S,
                        sum((value for _, value in values), Decimal(0)),
                    )
                )
        elif topic.startswith("allLiquidation.") and isinstance(data, list):
            symbol = topic.split(".", 1)[1]
            coverage_start = self._liquidation_coverage_start.setdefault(symbol, timestamp)
            values = self._liquidations.setdefault(symbol, deque())
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                price = _decimal(item.get("p"))
                size = _decimal(item.get("v"))
                side = item.get("S")
                event_at = _timestamp(item.get("T")) or timestamp
                if price is None or size is None or side not in {"Buy", "Sell"}:
                    continue
                notional = price * size
                values.append((event_at, notional))
                if self._stream_cache is not None:
                    self._stream_cache.record_liquidation(
                        PublicLiquidation(
                            symbol=symbol,
                            timestamp=event_at,
                            # Bybit allLiquidation `S` is the liquidated position side,
                            # not an aggressor/order side: Buy means a long was liquidated.
                            liquidated_position="LONG" if side == "Buy" else "SHORT",
                            price=price,
                            size=size,
                        )
                    )
            cutoff = timestamp.timestamp() - 60
            while values and values[0][0].timestamp() <= cutoff:
                values.popleft()
            if (timestamp - coverage_start).total_seconds() >= 60:
                observations.append(
                    (
                        symbol,
                        MonitoringMetric.LIQUIDATION_NOTIONAL_1M,
                        sum((value for _, value in values), Decimal(0)),
                    )
                )
        elif topic.startswith("orderbook.") and isinstance(data, Mapping):
            parts = topic.split(".")
            if len(parts) == 3:
                imbalance = self._update_orderbook(
                    parts[2], data, message_type=document.get("type")
                )
                if imbalance is not None:
                    observations.append(
                        (
                            parts[2],
                            MonitoringMetric.ORDERBOOK_IMBALANCE_L5,
                            imbalance,
                        )
                    )
        for symbol, metric, value in observations:
            events = self._engine.observe(
                symbol=symbol,
                metric=metric,
                value=value,
                observed_at=timestamp,
                confirmation_seconds=(
                    self._config.microstructure_confirmation_seconds
                    if metric
                    in {
                        MonitoringMetric.ORDERBOOK_IMBALANCE_L5,
                        MonitoringMetric.TRADE_DELTA_30S,
                        MonitoringMetric.SPREAD_BPS,
                    }
                    else 0
                ),
            )
            if events:
                await self._queue(events)

    async def _queue(self, events: tuple[ThresholdEvent, ...]) -> None:
        if not events:
            return
        # Accept the crossing before any notification delay or model work. This
        # makes the whole-version suspension durable even if a scheduled cycle
        # publishes another conclusion while the event is being handled.
        active_keys = {
            (active.analysis_id, active.symbol, active.directive.family_id)
            for active in self._engine.directives(include_suspended=True)
        }
        valid_events = tuple(
            event
            for event in events
            if (event.analysis_id, event.symbol, event.family_id) in active_keys
        )
        if not valid_events:
            # Direct callers and a freshly started monitor may not have loaded the
            # active set yet. Engine-produced events take the fast path above so a
            # concurrent scheduled publish cannot erase an already observed crossing.
            await self._refresh_directives()
            active_keys = {
                (active.analysis_id, active.symbol, active.directive.family_id)
                for active in self._engine.directives(include_suspended=True)
            }
            valid_events = tuple(
                event
                for event in events
                if (event.analysis_id, event.symbol, event.family_id) in active_keys
            )
        accepted = await self._record_events(valid_events)
        if not accepted:
            return
        await self._refresh_directives()
        symbol = events[0].symbol
        self._pending.setdefault(symbol, []).extend(accepted)
        if symbol not in self._flush_tasks:
            self._flush_tasks[symbol] = asyncio.create_task(self._flush(symbol))

    async def _flush(self, symbol: str) -> None:
        try:
            valid_events = tuple(self._pending.pop(symbol, []))
            latest_by_family: dict[tuple[str, str, str], ThresholdEvent] = {}
            for event in valid_events:
                latest_by_family[(event.analysis_id, event.symbol, event.family_id)] = event
            events = tuple(latest_by_family.values())
            if not events:
                return
            now = datetime.now(UTC)
            critical = any(event.critical for event in events)
            if not self._limiter.allow(now=now, critical=critical):
                logger.warning(
                    "realtime threshold wake exceeded advisory rate budget "
                    "symbol=%s analysis_id=%s",
                    symbol,
                    events[0].analysis_id,
                )
            if self._on_trigger is not None:
                try:
                    await self._on_trigger(events)
                except Exception:
                    logger.exception(
                        "realtime coalesced trigger callback failed symbol=%s", symbol
                    )
            logger.info(
                "realtime threshold wake symbol=%s events=%s critical=%s",
                symbol,
                len(events),
                critical,
            )
            try:
                await self._on_emergency(symbol, events)
            except Exception:
                logger.exception("realtime emergency callback failed symbol=%s", symbol)
            finally:
                # A successful emergency analysis publishes a new analysis_id and
                # therefore a fresh set. Failure leaves the old version suspended.
                await self._refresh_directives()
        finally:
            self._flush_tasks.pop(symbol, None)
            if self._pending.get(symbol):
                self._flush_tasks[symbol] = asyncio.create_task(self._flush(symbol))

    async def _record_events(
        self,
        events: tuple[ThresholdEvent, ...],
    ) -> tuple[ThresholdEvent, ...]:
        inserted: list[ThresholdEvent] = []
        for event in events:
            payload: dict[str, object] = {
                "event_id": event.event_id,
                "analysis_id": event.analysis_id,
                "symbol": event.symbol,
                "family_id": event.family_id,
                "metric": event.metric.value,
                "observed_value": str(event.observed_value),
                "threshold": str(event.threshold),
                "observed_at": event.observed_at.isoformat(),
                "reason": event.reason,
                "critical": event.critical,
                "matched_conditions": [
                    {
                        "metric": condition.metric.value,
                        "comparator": condition.comparator.value,
                        "threshold": str(condition.threshold),
                        "observed_value": str(condition.observed_value),
                    }
                    for condition in event.matched_conditions
                ],
            }
            if await self._store.record_threshold_event(
                event_id=event.event_id,
                analysis_id=event.analysis_id,
                symbol=event.symbol,
                family_id=event.family_id,
                observed_at=event.observed_at.isoformat(),
                critical=event.critical,
                payload=payload,
            ):
                inserted.append(event)
        return tuple(inserted)

    def _update_orderbook(
        self,
        symbol: str,
        data: Mapping[str, Any],
        *,
        message_type: object,
    ) -> Decimal | None:
        if message_type == "snapshot":
            bids: dict[Decimal, Decimal] = {}
            asks: dict[Decimal, Decimal] = {}
            self._orderbooks[symbol] = (bids, asks)
        elif symbol in self._orderbooks:
            bids, asks = self._orderbooks[symbol]
        else:
            return None
        _apply_level_updates(bids, data.get("b"))
        _apply_level_updates(asks, data.get("a"))
        bid_notional = _book_notional(bids, reverse=True, limit=5)
        ask_notional = _book_notional(asks, reverse=False, limit=5)
        total = bid_notional + ask_notional
        if total <= 0:
            return None
        return (bid_notional - ask_notional) / total


def _ticker_observations(
    symbol: str,
    ticker: Mapping[str, Any],
) -> list[tuple[str, MonitoringMetric, Decimal]]:
    observations: list[tuple[str, MonitoringMetric, Decimal]] = []
    for field, metric in (
        ("lastPrice", MonitoringMetric.LAST_PRICE),
        ("markPrice", MonitoringMetric.MARK_PRICE),
        ("openInterest", MonitoringMetric.OPEN_INTEREST),
        ("fundingRate", MonitoringMetric.FUNDING_RATE),
    ):
        value = _decimal(ticker.get(field))
        if value is not None:
            observations.append((symbol, metric, value))
    bid = _decimal(ticker.get("bid1Price"))
    ask = _decimal(ticker.get("ask1Price"))
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        midpoint = (bid + ask) / 2
        observations.append((symbol, MonitoringMetric.SPREAD_BPS, (ask - bid) / midpoint * 10_000))
    return observations


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _apply_level_updates(levels: dict[Decimal, Decimal], value: object) -> None:
    if not isinstance(value, list):
        return
    for row in value:
        if not isinstance(row, list) or len(row) < 2:
            continue
        price = _decimal(row[0])
        size = _decimal(row[1])
        if price is None or size is None:
            continue
        if size == 0:
            levels.pop(price, None)
        elif size > 0:
            levels[price] = size


def _book_notional(
    levels: Mapping[Decimal, Decimal], *, reverse: bool, limit: int
) -> Decimal:
    prices = sorted(levels, reverse=reverse)[:limit]
    return sum((price * levels[price] for price in prices), Decimal(0))
