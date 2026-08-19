from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bybit_signal.domain.enums import (
    CycleMode,
    CycleStatus,
    FailureStep,
    FailureWorkflow,
    RetryStatus,
)
from bybit_signal.domain.models import AnalysisCycleResult
from bybit_signal.failures import (
    failure_from_cycle,
    monitoring_failure,
    redact_sensitive_text,
    unexpected_failure,
)
from bybit_signal.storage.sqlite import SignalStore


def _failed_cycle() -> AnalysisCycleResult:
    now = datetime.now(UTC)
    return AnalysisCycleResult(
        selection_contract_version=4,
        analysis_id="cycle_failure_fixture",
        mode=CycleMode.SCHEDULED,
        status=CycleStatus.FAILED,
        started_at=now,
        completed_at=now,
        candidate_symbols=("CYSUSDT", "GPSUSDT", "TUTUSDT", "ALICEUSDT", "AKEUSDT"),
        conclusions=(),
        strong_signal_count=0,
        selected_symbols=(),
        selected_signal_count=0,
        context_sha256="f" * 64,
        diagnostics={
            "failures": {"CODEX": "CODEX_TIMEOUT: model exceeded 300 seconds"}
        },
    )


def test_cycle_failure_identifies_exact_step_and_retry_scope() -> None:
    event = failure_from_cycle(_failed_cycle())

    assert event.workflow is FailureWorkflow.SCHEDULED_ANALYSIS
    assert event.step is FailureStep.MODEL_ANALYSIS
    assert event.step_index == 3
    assert event.total_steps == 4
    assert event.completed_steps == (
        "全市场扫描与 Top5 过滤",
        "Top5 深度数据采集",
    )
    assert event.code == "CODEX_TIMEOUT"
    assert "300" in event.direct_cause
    assert "两个主信号" in event.impact


def test_unexpected_failure_redacts_telegram_token() -> None:
    event = unexpected_failure(
        workflow=FailureWorkflow.EMERGENCY_ANALYSIS,
        analysis_id="urgent_failure_fixture",
        cause=RuntimeError(
            "request used 1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef"
        ),
        symbol="CYSUSDT",
    )

    assert "1234567890" not in event.direct_cause
    assert "REDACTED_TELEGRAM_TOKEN" in event.direct_cause


def test_retry_diagnostics_redact_telegram_credentials() -> None:
    value = redact_sensitive_text(
        "TelegramError: https://api.telegram.org/bot1234567890:"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456/sendMessage failed"
    )

    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in value
    assert "/bot[REDACTED]/" in value


def test_monitoring_failure_surfaces_credit_exhaustion_as_direct_cause() -> None:
    event = monitoring_failure(
        _failed_cycle(),
        symbol="GPSUSDT",
        step_name="MODEL_MONITORING_REVIEW",
        cause=(
            "CodexAnalysisError: stdout={\"type\":\"error\","
            "\"message\":\"Your workspace is out of credits. "
            "Add credits to continue.\"}"
        ),
    )

    assert event.code == "CODEX_CREDITS_EXHAUSTED"
    assert event.direct_cause == "Codex 工作区额度已耗尽, 服务在模型开始分析前被拒绝。"
    assert "补充可用额度" in event.resolution


async def test_retry_job_claim_is_atomic_and_success_is_terminal(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    event = failure_from_cycle(_failed_cycle())

    first = await store.create_retry_job(event)
    duplicate = await store.create_retry_job(event)
    claimed = await store.claim_retry(first.token, requested_by=456)
    second_claim = await store.claim_retry(first.token, requested_by=456)

    assert duplicate.token == first.token
    assert claimed is not None and claimed.status is RetryStatus.RUNNING
    assert second_claim is None
    completed = await store.finish_retry(
        first.token,
        status=RetryStatus.SUCCEEDED,
        result_analysis_id="cycle_retry_success",
    )
    assert completed is not None and completed.status is RetryStatus.SUCCEEDED
    assert await store.claim_retry(first.token, requested_by=456) is None


async def test_running_retry_becomes_reclaimable_after_restart(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    event = failure_from_cycle(_failed_cycle())
    job = await store.create_retry_job(event)
    assert await store.claim_retry(job.token, requested_by=456) is not None

    assert await store.interrupt_running_retry_jobs() == 1
    interrupted = await store.retry_job(job.token)
    assert interrupted is not None and interrupted.status is RetryStatus.INTERRUPTED
    reclaimed = await store.claim_retry(job.token, requested_by=456)
    assert reclaimed is not None and reclaimed.status is RetryStatus.RUNNING
