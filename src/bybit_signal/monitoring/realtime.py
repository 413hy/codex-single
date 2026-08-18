from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import websockets

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import MonitoringMetric
from bybit_signal.monitoring.engine import ThresholdEngine, ThresholdEvent, WakeLimiter
from bybit_signal.storage.sqlite import SignalStore

EmergencyCallback = Callable[[str, tuple[ThresholdEvent, ...]], Awaitable[None]]
logger = logging.getLogger(__name__)


class RealtimeMonitor:
    def __init__(
        self,
        *,
        websocket_url: str,
        config: MonitoringConfig,
        store: SignalStore,
        on_emergency: EmergencyCallback,
    ) -> None:
        self._websocket_url = websocket_url
        self._config = config
        self._store = store
        self._on_emergency = on_emergency
        self._engine = ThresholdEngine()
        self._limiter = WakeLimiter(config)
        self._pending: dict[str, list[ThresholdEvent]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}

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
                    for index in range(0, len(topics), 10):
                        await websocket.send(
                            json.dumps({"op": "subscribe", "args": topics[index : index + 10]})
                        )
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
                            refresh_deadline = (
                                loop.time() + self._config.directive_refresh_seconds
                            )
                            if topics != previous_topics:
                                break
                        if message is None:
                            continue
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        await self._handle_message(message)
            except (OSError, websockets.WebSocketException, ValueError) as error:
                logger.warning("public WebSocket reconnecting after %s", type(error).__name__)
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

    async def _refresh_directives(self) -> None:
        self._engine.replace_conclusions(
            await self._store.active_monitoring_conclusions()
        )

    def _topics(self) -> list[str]:
        directives = self._engine.directives()
        ticker_symbols = {
            active.symbol
            for active in directives
            if active.directive.metric
            in {
                MonitoringMetric.LAST_PRICE,
                MonitoringMetric.MARK_PRICE,
                MonitoringMetric.SPREAD_BPS,
                MonitoringMetric.OPEN_INTEREST,
                MonitoringMetric.FUNDING_RATE,
            }
        }
        kline_intervals: dict[MonitoringMetric, str] = {
            MonitoringMetric.COMPLETED_5M_CLOSE: "5",
            MonitoringMetric.COMPLETED_15M_CLOSE: "15",
            MonitoringMetric.COMPLETED_1H_CLOSE: "60",
        }
        topics = [f"tickers.{symbol}" for symbol in sorted(ticker_symbols)]
        topics.extend(
            f"kline.{interval}.{active.symbol}"
            for active in directives
            if (interval := kline_intervals.get(active.directive.metric)) is not None
        )
        return sorted(set(topics))

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
                "5": MonitoringMetric.COMPLETED_5M_CLOSE,
                "15": MonitoringMetric.COMPLETED_15M_CLOSE,
                "60": MonitoringMetric.COMPLETED_1H_CLOSE,
            }.get(parts[1])
            if metric is not None:
                for item in data:
                    if isinstance(item, Mapping) and item.get("confirm") is True:
                        close = _decimal(item.get("close"))
                        if close is not None:
                            observations.append((parts[2], metric, close))
        for symbol, metric, value in observations:
            events = self._engine.observe(
                symbol=symbol,
                metric=metric,
                value=value,
                observed_at=timestamp,
            )
            if events:
                self._queue(events)

    def _queue(self, events: tuple[ThresholdEvent, ...]) -> None:
        symbol = events[0].symbol
        self._pending.setdefault(symbol, []).extend(events)
        if symbol not in self._flush_tasks:
            self._flush_tasks[symbol] = asyncio.create_task(self._flush(symbol))

    async def _flush(self, symbol: str) -> None:
        try:
            await asyncio.sleep(self._config.event_coalesce_seconds)
            await self._refresh_directives()
            active_keys = {
                (active.analysis_id, active.symbol, active.directive.family_id)
                for active in self._engine.directives()
            }
            events = tuple(
                event
                for event in self._pending.pop(symbol, [])
                if (event.analysis_id, event.symbol, event.family_id) in active_keys
            )
            if not events:
                return
            now = datetime.now(UTC)
            critical = any(event.critical for event in events)
            if self._limiter.allow(symbol, now=now, critical=critical):
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
            self._flush_tasks.pop(symbol, None)


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
