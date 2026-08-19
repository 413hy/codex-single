from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
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
from bybit_signal.providers.public_streams import PublicStreamCache


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
                    metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                    comparator=Comparator.GREATER_THAN,
                    threshold=Decimal("100"),
                    hysteresis=Decimal("1"),
                    valid_for_seconds=3600,
                    reason="short structure invalidation",
                    evidence_ids=(evidence_id,),
                    current_value=Decimal("99"),
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
        self.triggered_keys: set[tuple[str, str, str]] = set()
        self.triggered_versions: set[tuple[str, str]] = set()
        self.recorded_events: list[str] = []
        self.runtime_states: dict[str, str] = {}
        self.latest_success: object | None = None

    async def active_monitoring_conclusions(self) -> tuple[SignalConclusion, ...]:
        index = min(self.calls, len(self._responses) - 1)
        result = self._responses[index]
        self.calls += 1
        if self.calls >= 2 and self._stop_event is not None:
            self._stop_event.set()
        return result

    async def triggered_directive_keys(
        self, analysis_ids: tuple[str, ...]
    ) -> frozenset[tuple[str, str, str]]:
        return frozenset(key for key in self.triggered_keys if key[0] in analysis_ids)

    async def triggered_analysis_symbol_keys(
        self, analysis_ids: tuple[str, ...]
    ) -> frozenset[tuple[str, str]]:
        return frozenset(key for key in self.triggered_versions if key[0] in analysis_ids)

    async def record_threshold_event(self, **payload: object) -> bool:
        event_id = str(payload["event_id"])
        version_key = (str(payload["analysis_id"]), str(payload["symbol"]))
        if event_id in self.recorded_events or version_key in self.triggered_versions:
            return False
        self.recorded_events.append(event_id)
        self.triggered_versions.add(version_key)
        self.triggered_keys.add(
            (
                str(payload["analysis_id"]),
                str(payload["symbol"]),
                str(payload["family_id"]),
            )
        )
        return True

    async def runtime_state(self, key: str) -> str | None:
        return self.runtime_states.get(key)

    async def set_runtime_state(self, key: str, value: str) -> None:
        self.runtime_states[key] = value

    async def delete_runtime_state(self, key: str) -> None:
        self.runtime_states.pop(key, None)

    async def latest_scheduled_cycle(self, *, successful_only: bool = False) -> object | None:
        assert successful_only is True
        return self.latest_success


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
        soft_model_wakes_per_hour=10,
        reconnect_delay_seconds=0.01,
        directive_refresh_seconds=0.01,
        microstructure_confirmation_seconds=3,
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
    assert websocket.sent == [
        {
            "op": "subscribe",
            "args": ["kline.5.TUTUSDT"],
        }
    ]
    assert monitor._engine.directives() == ()


@pytest.mark.asyncio
async def test_emergency_review_atomically_replaces_old_thresholds() -> None:
    now = datetime.now(UTC)
    scheduled = _conclusion("TUTUSDT", now)
    fresh_directive = MonitoringDirective(
        family_id="tutusdt.reversal.confirmation.1m",
        metric=MonitoringMetric.COMPLETED_1M_CLOSE,
        comparator=Comparator.LESS_THAN,
        threshold=Decimal("97"),
        hysteresis=Decimal("0.25"),
        valid_for_seconds=3600,
        reason="fresh threshold derived by emergency analysis",
        evidence_ids=("TUTUSDT.PA.5M",),
    )
    emergency = scheduled.model_copy(
        update={
            "analysis_id": "emergency_cycle_02",
            "assessment": scheduled.assessment.model_copy(
                update={"monitoring_directives": (fresh_directive,)}
            ),
        }
    )
    store = _ChangingStore([(scheduled,), (emergency,)])
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )

    await monitor._refresh_directives()
    first = monitor._engine.directives()
    await monitor._refresh_directives()
    second = monitor._engine.directives()

    assert [active.directive.family_id for active in first] == [
        "tutusdt.price.invalidation.short"
    ]
    assert [active.directive.family_id for active in second] == [
        "tutusdt.reversal.confirmation.1m"
    ]
    assert all(
        active.directive.family_id != "tutusdt.price.invalidation.short"
        for active in second
    )


@pytest.mark.asyncio
async def test_completed_30m_threshold_subscribes_and_wakes_one_review() -> None:
    now = datetime.now(UTC)
    base = _conclusion("TUTUSDT", now)
    directive = base.assessment.monitoring_directives[0].model_copy(
        update={
            "family_id": "tutusdt.invalidation.30m",
            "metric": MonitoringMetric.COMPLETED_30M_CLOSE,
            "confirmation": "completed_30m",
        }
    )
    conclusion = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    triggered: list[tuple[ThresholdEvent, ...]] = []

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        triggered.append(events)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=_ChangingStore([(conclusion,)]),  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
        on_trigger=on_trigger,
    )
    await monitor._refresh_directives()

    assert monitor._topics() == ["kline.30.TUTUSDT"]
    event_at = now + timedelta(minutes=30)
    await monitor._handle_message(
        json.dumps(
            {
                "topic": "kline.30.TUTUSDT",
                "ts": int(event_at.timestamp() * 1000),
                "data": [{"confirm": True, "close": "101"}],
            }
        )
    )
    await asyncio.gather(*monitor._flush_tasks.values())

    assert len(triggered) == 1
    assert triggered[0][0].metric is MonitoringMetric.COMPLETED_30M_CLOSE


@pytest.mark.asyncio
async def test_failed_emergency_keeps_entire_old_version_suspended_after_restart() -> None:
    now = datetime.now(UTC)
    scheduled = _conclusion("TUTUSDT", now)
    store = _ChangingStore([(scheduled,)])
    store.triggered_versions.add((scheduled.analysis_id, "TUTUSDT"))

    first_monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    await first_monitor._refresh_directives()
    restarted_monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    await restarted_monitor._refresh_directives()

    assert first_monitor._engine.directives() == ()
    assert restarted_monitor._engine.directives() == ()
    assert len(restarted_monitor._engine.directives(include_suspended=True)) == 1


@pytest.mark.asyncio
async def test_new_analysis_version_resumes_after_old_version_was_suspended() -> None:
    now = datetime.now(UTC)
    scheduled = _conclusion("TUTUSDT", now)
    fresh_directive = scheduled.assessment.monitoring_directives[0].model_copy(
        update={
            "family_id": "tutusdt.reversal.confirmation.1m",
            "metric": MonitoringMetric.COMPLETED_1M_CLOSE,
            "threshold": Decimal("97"),
        }
    )
    emergency = scheduled.model_copy(
        update={
            "analysis_id": "urgent_cycle_02",
            "assessment": scheduled.assessment.model_copy(
                update={"monitoring_directives": (fresh_directive,)}
            ),
        }
    )
    store = _ChangingStore([(scheduled,)])
    store.triggered_versions.add((scheduled.analysis_id, "TUTUSDT"))
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )

    await monitor._refresh_directives()
    assert monitor._engine.directives() == ()
    store._responses = [(emergency,)]
    store.calls = 0
    await monitor._refresh_directives()

    active = monitor._engine.directives()
    assert len(active) == 1
    assert active[0].analysis_id == "urgent_cycle_02"
    assert active[0].directive.family_id == "tutusdt.reversal.confirmation.1m"


@pytest.mark.asyncio
async def test_scheduled_refresh_pause_survives_restart_until_explicit_resume() -> None:
    now = datetime.now(UTC)
    conclusion = _conclusion("TUTUSDT", now)
    store = _ChangingStore([(conclusion,)])
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    await monitor._refresh_directives()
    assert len(monitor._engine.directives()) == 1

    paused_at = now + timedelta(minutes=20)
    await monitor.pause_for_scheduled_refresh(paused_at=paused_at)
    assert monitor._engine.directives() == ()

    restarted = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    await restarted._refresh_directives()
    assert restarted._engine.directives() == ()

    store.latest_success = SimpleNamespace(completed_at=paused_at + timedelta(seconds=1))
    await restarted._refresh_directives()
    assert restarted._engine.directives() == ()
    assert store.runtime_states

    await restarted.resume_after_scheduled_success()
    assert len(restarted._engine.directives()) == 1
    assert store.runtime_states == {}


@pytest.mark.asyncio
async def test_scheduled_refresh_waits_for_pre_pause_emergency_to_finish() -> None:
    store = _ChangingStore([()])
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=store,  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    release = asyncio.Event()

    async def pending_review() -> None:
        try:
            await release.wait()
        finally:
            monitor._flush_tasks.pop("TUTUSDT", None)

    monitor._flush_tasks["TUTUSDT"] = asyncio.create_task(pending_review())

    waiter = asyncio.create_task(monitor.wait_until_idle())
    await asyncio.sleep(0)
    assert waiter.done() is False
    release.set()
    await waiter


@pytest.mark.asyncio
async def test_coalesced_event_is_dropped_after_signal_exits() -> None:
    now = datetime.now(UTC)
    calls: list[str] = []
    store = _ChangingStore([()])
    config = MonitoringConfig.model_construct(
        enabled=True,
        soft_model_wakes_per_hour=10,
        reconnect_delay_seconds=0.01,
        directive_refresh_seconds=0.01,
        microstructure_confirmation_seconds=3,
    )

    async def on_emergency(symbol: str, _events: tuple[ThresholdEvent, ...]) -> None:
        calls.append(symbol)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=config,
        store=store,  # type: ignore[arg-type]
        on_emergency=on_emergency,
    )
    await monitor._queue(
        (
            ThresholdEvent(
                analysis_id="scheduled_cycle_01",
                symbol="TUTUSDT",
                family_id="tutusdt.price.invalidation.short",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
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


@pytest.mark.asyncio
async def test_trigger_notification_is_followed_by_emergency_analysis() -> None:
    now = datetime.now(UTC)
    triggered: list[str] = []
    emergency_calls: list[str] = []

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        triggered.append(events[0].symbol)

    async def on_emergency(symbol: str, _events: tuple[ThresholdEvent, ...]) -> None:
        emergency_calls.append(symbol)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig.model_construct(
            enabled=True,
            soft_model_wakes_per_hour=10,
            reconnect_delay_seconds=0.01,
            directive_refresh_seconds=30,
            microstructure_confirmation_seconds=3,
        ),
        store=_ChangingStore([(_conclusion("EDENUSDT", now),)]),  # type: ignore[arg-type]
        on_emergency=on_emergency,
        on_trigger=on_trigger,
    )
    await monitor._queue(
        (
            ThresholdEvent(
                analysis_id="scheduled_cycle_01",
                symbol="EDENUSDT",
                family_id="edenusdt.price.invalidation.short",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                observed_value=Decimal("99"),
                threshold=Decimal("100"),
                observed_at=now,
                reason="scheduled cycle is already refreshing the market",
                critical=False,
            ),
        )
    )

    await asyncio.gather(*monitor._flush_tasks.values())

    assert triggered == ["EDENUSDT"]
    assert emergency_calls == ["EDENUSDT"]
    assert monitor._pending == {}
    assert monitor._flush_tasks == {}


@pytest.mark.asyncio
async def test_soft_rate_budget_warns_but_does_not_drop_confirmed_cycle() -> None:
    now = datetime.now(UTC)
    order: list[str] = []
    conclusion = _conclusion("EDENUSDT", now)
    config = MonitoringConfig.model_construct(
        enabled=True,
        soft_model_wakes_per_hour=1,
        reconnect_delay_seconds=0.01,
        directive_refresh_seconds=30,
        microstructure_confirmation_seconds=3,
    )

    async def on_trigger(_events: tuple[ThresholdEvent, ...]) -> None:
        order.append("notify")

    async def on_emergency(_symbol: str, _events: tuple[ThresholdEvent, ...]) -> None:
        order.append("analyze")

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=config,
        store=_ChangingStore([(conclusion,)]),  # type: ignore[arg-type]
        on_emergency=on_emergency,
        on_trigger=on_trigger,
    )
    assert monitor._limiter.allow(now=now, critical=False)
    await monitor._queue(
        (
            ThresholdEvent(
                analysis_id=conclusion.analysis_id,
                symbol="EDENUSDT",
                family_id="edenusdt.price.invalidation.short",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                observed_value=Decimal("101"),
                threshold=Decimal("100"),
                observed_at=now + timedelta(seconds=1),
                reason="confirmed crossing must complete the lifecycle",
                critical=False,
            ),
        )
    )
    await asyncio.gather(*monitor._flush_tasks.values())

    assert order == ["notify", "analyze"]
    assert monitor._engine.directives() == ()


@pytest.mark.asyncio
async def test_first_crossing_is_persisted_and_notified_once_per_version() -> None:
    now = datetime.now(UTC)
    triggered: list[tuple[ThresholdEvent, ...]] = []
    emergency_calls: list[tuple[ThresholdEvent, ...]] = []
    conclusion = _conclusion("ACEUSDT", now)

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        triggered.append(events)

    async def on_emergency(_symbol: str, events: tuple[ThresholdEvent, ...]) -> None:
        emergency_calls.append(events)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig.model_construct(
            enabled=True,
            soft_model_wakes_per_hour=10,
            reconnect_delay_seconds=0.01,
            directive_refresh_seconds=30,
            microstructure_confirmation_seconds=3,
        ),
        store=_ChangingStore([(conclusion,)]),  # type: ignore[arg-type]
        on_emergency=on_emergency,
        on_trigger=on_trigger,
    )
    events = tuple(
        ThresholdEvent(
            analysis_id="scheduled_cycle_01",
            symbol="ACEUSDT",
            family_id="aceusdt.price.invalidation.short",
            metric=MonitoringMetric.COMPLETED_5M_CLOSE,
            observed_value=Decimal(value),
            threshold=Decimal("100"),
            observed_at=now + timedelta(milliseconds=index),
            reason="same semantic family crosses repeatedly",
            critical=False,
        )
        for index, value in enumerate(("101", "102", "103"), start=1)
    )
    await monitor._queue(events)
    await asyncio.gather(*monitor._flush_tasks.values())

    assert len(triggered) == 1
    assert len(triggered[0]) == 1
    assert triggered[0][0].observed_value == Decimal("101")
    assert emergency_calls == triggered


@pytest.mark.asyncio
async def test_sibling_event_during_emergency_is_discarded_with_old_version() -> None:
    now = datetime.now(UTC)
    base = _conclusion("ACEUSDT", now)
    second_directive = base.assessment.monitoring_directives[0].model_copy(
        update={
            "family_id": "aceusdt.target.last",
            "threshold": Decimal("102"),
        }
    )
    conclusion = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "monitoring_directives": (
                        *base.assessment.monitoring_directives,
                        second_directive,
                    )
                }
            )
        }
    )
    store = _ChangingStore([(conclusion,)])
    emergency_started = asyncio.Event()
    release_emergency = asyncio.Event()

    async def on_emergency(_symbol: str, _events: tuple[ThresholdEvent, ...]) -> None:
        emergency_started.set()
        await release_emergency.wait()

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig.model_construct(
            enabled=True,
            soft_model_wakes_per_hour=10,
            reconnect_delay_seconds=0.01,
            directive_refresh_seconds=30,
            microstructure_confirmation_seconds=3,
        ),
        store=store,  # type: ignore[arg-type]
        on_emergency=on_emergency,
    )

    def event(family_id: str, seconds: int) -> ThresholdEvent:
        return ThresholdEvent(
            analysis_id="scheduled_cycle_01",
            symbol="ACEUSDT",
            family_id=family_id,
            metric=MonitoringMetric.COMPLETED_5M_CLOSE,
            observed_value=Decimal("103"),
            threshold=Decimal("102"),
            observed_at=now + timedelta(seconds=seconds),
            reason="fixture crossing",
            critical=True,
        )

    await monitor._queue((event("aceusdt.price.invalidation.short", 1),))
    await emergency_started.wait()
    await monitor._queue((event("aceusdt.target.last", 2),))
    release_emergency.set()
    while monitor._flush_tasks:
        await asyncio.gather(*tuple(monitor._flush_tasks.values()))

    assert len(store.recorded_events) == 1
    assert monitor._pending == {}
    assert monitor._flush_tasks == {}


@pytest.mark.asyncio
async def test_orderbook_delta_updates_snapshot_instead_of_becoming_partial_book() -> None:
    now = datetime.now(UTC)
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=_ChangingStore([()]),  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )
    await monitor._handle_message(
        json.dumps(
            {
                "topic": "orderbook.50.ACEUSDT",
                "type": "snapshot",
                "ts": int(now.timestamp() * 1000),
                "data": {
                    "b": [["100", "10"], ["99", "10"]],
                    "a": [["101", "10"], ["102", "10"]],
                },
            }
        )
    )
    await monitor._handle_message(
        json.dumps(
            {
                "topic": "orderbook.50.ACEUSDT",
                "type": "delta",
                "ts": int((now + timedelta(milliseconds=1)).timestamp() * 1000),
                "data": {"b": [["100", "0"], ["98", "20"]]},
            }
        )
    )

    bids, asks = monitor._orderbooks["ACEUSDT"]
    assert Decimal("100") not in bids
    assert bids[Decimal("98")] == Decimal("20")
    assert asks == {Decimal("101"): Decimal("10"), Decimal("102"): Decimal("10")}


def test_orderbook_delta_before_snapshot_is_ignored() -> None:
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=_ChangingStore([()]),  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
    )

    value = monitor._update_orderbook(
        "ACEUSDT",
        {"b": [["100", "1"]], "a": [["101", "1"]]},
        message_type="delta",
    )

    assert value is None
    assert "ACEUSDT" not in monitor._orderbooks


@pytest.mark.asyncio
async def test_high_frequency_orderbook_is_confirmed_then_consumed_once() -> None:
    now = datetime.now(UTC)
    base = _conclusion("ACEUSDT", now)
    evidence_id = "ACEUSDT.PA.5M"
    directive = MonitoringDirective(
        family_id="aceusdt.book.sell_pressure",
        metric=MonitoringMetric.ORDERBOOK_IMBALANCE_L5,
        comparator=Comparator.LESS_THAN,
        threshold=Decimal("-0.2"),
        hysteresis=Decimal("0.1"),
        valid_for_seconds=3600,
        reason="persistent sell-side depth pressure",
        evidence_ids=(evidence_id,),
        current_value=Decimal("0"),
    )
    conclusion = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    triggered: list[tuple[ThresholdEvent, ...]] = []
    emergency_calls: list[tuple[ThresholdEvent, ...]] = []

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        triggered.append(events)

    async def on_emergency(_symbol: str, events: tuple[ThresholdEvent, ...]) -> None:
        emergency_calls.append(events)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig.model_construct(
            enabled=True,
            soft_model_wakes_per_hour=10,
            reconnect_delay_seconds=0.01,
            directive_refresh_seconds=30,
            microstructure_confirmation_seconds=3,
        ),
        store=_ChangingStore([(conclusion,)]),  # type: ignore[arg-type]
        on_emergency=on_emergency,
        on_trigger=on_trigger,
    )
    await monitor._refresh_directives()
    assert len(monitor._engine.directives()) == 1

    async def book_message(kind: str, size: str, offset_ms: int) -> None:
        await monitor._handle_message(
            json.dumps(
                {
                    "topic": "orderbook.50.ACEUSDT",
                    "type": kind,
                    "ts": int((now + timedelta(milliseconds=offset_ms)).timestamp() * 1000),
                    "data": {
                        "b": [["100", size]],
                        "a": [["101", "10"]] if kind == "snapshot" else [],
                    },
                }
            )
        )

    await book_message("snapshot", "10", 0)
    for offset, size in enumerate(("1", "1", "1", "1"), start=1):
        await book_message("delta", size, offset * 1000)
    await asyncio.gather(*monitor._flush_tasks.values())

    assert len(triggered) == 1
    assert len(triggered[0]) == 1
    assert triggered[0][0].family_id == "aceusdt.book.sell_pressure"
    assert emergency_calls == triggered

    await book_message("delta", "10", 5_000)
    await book_message("delta", "1", 9_000)
    await asyncio.gather(*monitor._flush_tasks.values())

    assert len(triggered) == 1
    assert len(emergency_calls) == 1


@pytest.mark.asyncio
async def test_trade_delta_waits_for_full_window_before_threshold_confirmation() -> None:
    now = datetime.now(UTC)
    base = _conclusion("ACEUSDT", now)
    directive = MonitoringDirective(
        family_id="aceusdt.trade.sell_pressure",
        metric=MonitoringMetric.TRADE_DELTA_30S,
        comparator=Comparator.LESS_THAN,
        threshold=Decimal("-100"),
        hysteresis=Decimal("50"),
        valid_for_seconds=3600,
        reason="persistent qualified sell-side trade delta",
        evidence_ids=("ACEUSDT.PA.5M",),
        current_value=Decimal("100"),
    )
    conclusion = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={"monitoring_directives": (directive,)}
            )
        }
    )
    triggered: list[tuple[ThresholdEvent, ...]] = []

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        triggered.append(events)

    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig.model_construct(
            enabled=True,
            soft_model_wakes_per_hour=10,
            reconnect_delay_seconds=0.01,
            directive_refresh_seconds=30,
            microstructure_confirmation_seconds=3,
        ),
        store=_ChangingStore([(conclusion,)]),  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
        on_trigger=on_trigger,
    )
    await monitor._refresh_directives()
    assert len(monitor._engine.directives()) == 1

    async def trade_message(offset_seconds: int, size: str) -> None:
        event_at = now + timedelta(seconds=offset_seconds)
        await monitor._handle_message(
            json.dumps(
                {
                    "topic": "publicTrade.ACEUSDT",
                    "ts": int(event_at.timestamp() * 1000),
                    "data": [
                        {
                            "T": int(event_at.timestamp() * 1000),
                            "S": "Sell",
                            "p": "1",
                            "v": size,
                        }
                    ],
                }
            )
        )

    await trade_message(1, "200")
    await trade_message(20, "200")
    assert triggered == []
    assert monitor._pending == {}

    await trade_message(31, "200")
    await trade_message(35, "200")
    await asyncio.gather(*monitor._flush_tasks.values())

    assert len(triggered) == 1
    assert triggered[0][0].family_id == "aceusdt.trade.sell_pressure"


@pytest.mark.asyncio
async def test_bybit_liquidation_side_is_position_side_not_order_side() -> None:
    now = datetime.now(UTC)
    cache = PublicStreamCache()
    monitor = RealtimeMonitor(
        websocket_url="wss://example.invalid",
        config=MonitoringConfig(),
        store=_ChangingStore([()]),  # type: ignore[arg-type]
        on_emergency=lambda _symbol, _events: asyncio.sleep(0),
        stream_cache=cache,
    )
    cache.mark_subscribed("TUTUSDT", at=now)

    await monitor._handle_message(
        json.dumps(
            {
                "topic": "allLiquidation.TUTUSDT",
                "ts": int(now.timestamp() * 1000),
                "data": [
                    {
                        "T": int(now.timestamp() * 1000),
                        "S": "Buy",
                        "p": "1",
                        "v": "1000",
                    }
                ],
            }
        )
    )

    window = cache.liquidation_window("TUTUSDT", seconds=60, now=now)
    assert window.long_notional == Decimal("1000")
    assert window.short_notional == Decimal(0)
