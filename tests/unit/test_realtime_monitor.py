from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import (
    Comparator,
    MonitoringMetric,
    PriceType,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    CanonicalPrice,
    MonitoringDirective,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.monitoring.engine import ThresholdEvent
from bybit_signal.monitoring.realtime import RealtimeMonitor


def _conclusion(symbol: str, now: datetime) -> SignalConclusion:
    evidence_id = f"{symbol}.PA.5M"
    return SignalConclusion(
        analysis_id="scheduled_cycle_01",
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=Decimal("99"),
            timestamp=now,
        ),
        assessment=CandidateAssessment(
            symbol=symbol,
            strength=SignalStrength.WATCH,
            market_state="realtime monitor fixture",
            summary="realtime monitor fixture",
            details="realtime monitor fixture details",
            evidence_ids=(evidence_id,),
            monitoring_directives=(
                MonitoringDirective(
                    family_id=f"{symbol.lower()}.price.invalidation.short",
                    metric=MonitoringMetric.LAST_PRICE,
                    comparator=Comparator.GREATER_THAN,
                    threshold=Decimal("100"),
                    hysteresis=Decimal("1"),
                    valid_for_seconds=3600,
                    reason="short structure invalidation",
                    evidence_ids=(evidence_id,),
                ),
            ),
        ),
        tracking_status=TrackingStatus.MAINTAINED,
        comparison_with_previous="fixture remains active",
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence available",
                evidence_ids=(evidence_id,),
            ),
        ),
    )


class _ChangingStore:
    def __init__(
        self,
        responses: list[tuple[SignalConclusion, ...]],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._responses = responses
        self._stop_event = stop_event
        self.calls = 0

    async def active_monitoring_conclusions(self) -> tuple[SignalConclusion, ...]:
        index = min(self.calls, len(self._responses) - 1)
        result = self._responses[index]
        self.calls += 1
        if self.calls >= 2 and self._stop_event is not None:
            self._stop_event.set()
        return result


class _BusyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        await asyncio.sleep(0)
        return json.dumps({"op": "pong"})


class _ConnectionContext:
    def __init__(self, websocket: _BusyWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _BusyWebSocket:
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_directives_refresh_even_when_websocket_is_continuously_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    stop_event = asyncio.Event()
    store = _ChangingStore([(_conclusion("TUTUSDT", now),), ()], stop_event=stop_event)
    config = MonitoringConfig.model_construct(
        enabled=True,
        event_coalesce_seconds=0.01,
        symbol_cooldown_seconds=1,
        soft_model_wakes_per_hour=10,
        reconnect_delay_seconds=0.01,
        directive_refresh_seconds=0.01,
    )
    websocket = _BusyWebSocket()
    monkeypatch.setattr(
        "bybit_signal.monitoring.realtime.websockets.connect",
        lambda *_args, **_kwargs: _ConnectionContext(websocket),
    )
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=config,
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )

    await asyncio.wait_for(monitor.run(stop_event), timeout=1)

    assert store.calls >= 2
    assert websocket.sent == [{"op": "subscribe", "args": ["tickers.TUTUSDT"]}]
    assert monitor._engine.directives() == ()


@pytest.mark.asyncio
async def test_coalesced_event_is_dropped_after_signal_exits() -> None:
    now = datetime.now(UTC)
    calls: list[str] = []
    store = _ChangingStore([()])
    config = MonitoringConfig.model_construct(
        enabled=True,
        event_coalesce_seconds=0.01,
        symbol_cooldown_seconds=1,
        soft_model_wakes_per_hour=10,
        reconnect_delay_seconds=0.01,
        directive_refresh_seconds=0.01,
    )

    async def on_emergency(symbol: str, _events: tuple[ThresholdEvent, ...]) -> None:
        calls.append(symbol)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=config,
        store=store,  # type: ignore[arg-type]
        on_emergency=on_emergency,
    )
    monitor._queue(
        (
            ThresholdEvent(
                analysis_id="scheduled_cycle_01",
                symbol="TUTUSDT",
                family_id="tutusdt.price.invalidation.short",
                metric=MonitoringMetric.LAST_PRICE,
                observed_value=Decimal("101"),
                threshold=Decimal("100"),
                observed_at=now,
                reason="short structure invalidation",
                critical=True,
            ),
        )
    )

    await asyncio.gather(*monitor._flush_tasks.values())

    assert calls == []
    assert monitor._pending == {}
