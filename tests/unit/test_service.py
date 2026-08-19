from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bybit_signal.domain.enums import CycleStatus
from bybit_signal.service import (
    _monitoring_failure_step,
    _next_half_hour,
    _scheduled_cycles,
    _scheduled_pause_at,
)


def test_activation_repair_exhaustion_is_reported_as_activation_step() -> None:
    assert (
        _monitoring_failure_step("model rejected activation repair: no safe anchor")
        == "MECHANICAL_ACTIVATION"
    )


def test_initial_model_rejection_is_reported_as_review_step() -> None:
    assert (
        _monitoring_failure_step("model rejected candidate thresholds: no safe anchor")
        == "MODEL_MONITORING_REVIEW"
    )


class _Monitor:
    def __init__(self) -> None:
        self.waits = 0
        self.pauses = 0
        self.resumes = 0

    async def wait_until_idle(self) -> None:
        self.waits += 1

    async def pause_for_scheduled_refresh(self) -> None:
        self.pauses += 1

    async def resume_after_scheduled_success(self) -> None:
        self.resumes += 1

    async def refresh_now(self) -> None:
        return None


class _CycleService:
    def __init__(self, status: CycleStatus) -> None:
        self.status = status
        self.calls = 0

    async def run_cycle(self) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            analysis_id=f"cycle_fixture_{self.calls:02d}",
            status=self.status,
            selected_signal_count=2 if self.status is CycleStatus.SUCCESS else 0,
        )

    async def review_and_activate_monitoring(
        self, _result: SimpleNamespace
    ) -> SimpleNamespace:
        return SimpleNamespace(errors={}, status="READY", committed=True)


class _OutcomeEvaluator:
    async def settle_ready(self) -> None:
        return None


class _Telegram:
    def __init__(self) -> None:
        self.cycles: list[object] = []
        self.failures: list[object] = []

    async def deliver_cycle(self, cycle: object) -> None:
        self.cycles.append(cycle)

    async def deliver_failure_event(self, event: object) -> None:
        self.failures.append(event)


def test_scheduled_pause_is_at_natural_boundary_without_blind_spot() -> None:
    now = datetime(2026, 8, 18, 12, 8, 13, tzinfo=UTC)
    scheduled = _next_half_hour(now)

    assert scheduled == datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    assert _scheduled_pause_at(scheduled) == scheduled


@pytest.mark.asyncio
async def test_prior_thresholds_pause_only_when_next_cycle_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = _Monitor()
    cycle_service = _CycleService(CycleStatus.SUCCESS)
    runtime = SimpleNamespace(
        monitor=monitor,
        cycle_service=cycle_service,
        outcome_evaluator=_OutcomeEvaluator(),
        telegram=None,
        settings=SimpleNamespace(telegram=SimpleNamespace(allowed_chat_ids=())),
    )
    waited_targets: list[datetime] = []

    async def run_one_natural_boundary(
        _stop_event: asyncio.Event,
        target: datetime,
    ) -> bool:
        waited_targets.append(target)
        return len(waited_targets) > 1

    monkeypatch.setattr("bybit_signal.service._wait_until", run_one_natural_boundary)
    await _scheduled_cycles(runtime, asyncio.Event())  # type: ignore[arg-type]

    assert cycle_service.calls == 2
    assert monitor.pauses == 1
    assert waited_targets[0].minute in {0, 30}
    assert _scheduled_pause_at(waited_targets[0]) == waited_targets[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_resumes"),
    ((CycleStatus.SUCCESS, 1), (CycleStatus.FAILED, 0)),
)
async def test_scheduled_cycle_only_resumes_thresholds_after_success(
    monkeypatch: pytest.MonkeyPatch,
    status: CycleStatus,
    expected_resumes: int,
) -> None:
    monitor = _Monitor()
    cycle_service = _CycleService(status)
    runtime = SimpleNamespace(
        monitor=monitor,
        cycle_service=cycle_service,
        outcome_evaluator=_OutcomeEvaluator(),
        telegram=None,
        settings=SimpleNamespace(telegram=SimpleNamespace(allowed_chat_ids=())),
    )

    async def stop_before_next_window(
        _stop_event: asyncio.Event,
        _target: datetime,
    ) -> bool:
        return True

    monkeypatch.setattr("bybit_signal.service._wait_until", stop_before_next_window)
    await _scheduled_cycles(runtime, asyncio.Event())  # type: ignore[arg-type]

    assert cycle_service.calls == 1
    assert monitor.waits == 0
    assert monitor.resumes == expected_resumes


async def test_scheduled_cycle_keeps_freeze_when_all_monitoring_reviews_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedMonitoringService(_CycleService):
        async def review_and_activate_monitoring(
            self, _result: SimpleNamespace
        ) -> SimpleNamespace:
            return SimpleNamespace(
                errors={"GPSUSDT": "model monitoring review failed"},
                status="FAILED",
                committed=True,
                diagnostics={"step": "MODEL_MONITORING_REVIEW"},
            )

    monitor = _Monitor()
    runtime = SimpleNamespace(
        monitor=monitor,
        cycle_service=FailedMonitoringService(CycleStatus.SUCCESS),
        outcome_evaluator=_OutcomeEvaluator(),
        telegram=None,
        settings=SimpleNamespace(telegram=SimpleNamespace(allowed_chat_ids=())),
    )

    async def stop_before_next_window(
        _stop_event: asyncio.Event,
        _target: datetime,
    ) -> bool:
        return True

    monkeypatch.setattr("bybit_signal.service._wait_until", stop_before_next_window)
    await _scheduled_cycles(runtime, asyncio.Event())  # type: ignore[arg-type]

    assert monitor.resumes == 0


async def test_monitoring_crash_does_not_relabel_delivered_signal_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CrashingMonitoringService(_CycleService):
        async def review_and_activate_monitoring(
            self, _result: SimpleNamespace
        ) -> SimpleNamespace:
            raise RuntimeError("monitoring storage unavailable")

    monitor = _Monitor()
    cycle_service = CrashingMonitoringService(CycleStatus.SUCCESS)
    telegram = _Telegram()
    runtime = SimpleNamespace(
        monitor=monitor,
        cycle_service=cycle_service,
        outcome_evaluator=_OutcomeEvaluator(),
        telegram=telegram,
        settings=SimpleNamespace(telegram=SimpleNamespace(allowed_chat_ids=())),
    )

    async def stop_before_next_window(
        _stop_event: asyncio.Event,
        _target: datetime,
    ) -> bool:
        return True

    monkeypatch.setattr("bybit_signal.service._wait_until", stop_before_next_window)
    await _scheduled_cycles(runtime, asyncio.Event())  # type: ignore[arg-type]

    assert len(telegram.cycles) == 1
    assert len(telegram.failures) == 1
    failure = telegram.failures[0]
    assert failure.workflow.value == "MONITORING_SETUP"  # type: ignore[attr-defined]
    assert "已有成功信号保持不变" in failure.impact  # type: ignore[attr-defined]
    assert monitor.resumes == 0
