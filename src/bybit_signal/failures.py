from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from bybit_signal.domain.enums import (
    CycleMode,
    FailureStep,
    FailureWorkflow,
    RetryAction,
)
from bybit_signal.domain.models import AnalysisCycleResult, FailureEvent

_TOKEN_PATTERN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")
_BOT_URL_PATTERN = re.compile(r"/bot[^/\s]+/")


class RetrySuperseded(RuntimeError):
    """Raised when a newer authoritative analysis makes a retry unsafe."""


def redact_sensitive_text(value: object) -> str:
    """Return bounded diagnostic text with known credential forms removed."""

    return _sanitize(str(value))


def failure_from_cycle(cycle: AnalysisCycleResult) -> FailureEvent:
    """Translate cycle diagnostics into a stable, user-actionable failure contract."""

    failures = _failure_mapping(cycle.diagnostics.get("failures"))
    key, raw = _primary_failure(failures)
    step = _cycle_failure_step(key, raw)
    workflow = (
        FailureWorkflow.SCHEDULED_ANALYSIS
        if cycle.mode is CycleMode.SCHEDULED
        else FailureWorkflow.EMERGENCY_ANALYSIS
    )
    total_steps = 4 if cycle.mode is CycleMode.SCHEDULED else 3
    step_index = _step_index(step, workflow)
    code, cause = _split_failure(raw, fallback=key or "UNCLASSIFIED_FAILURE")
    symbol = _diagnostic_symbol(cycle)
    completed = _completed_steps(step_index, workflow)
    resolution = _resolution_for(code, step, workflow)
    return FailureEvent(
        failure_id=_failure_id(cycle.analysis_id, workflow, step, symbol, cause),
        analysis_id=cycle.analysis_id,
        occurred_at=cycle.completed_at,
        workflow=workflow,
        step=step,
        step_index=step_index,
        total_steps=total_steps,
        completed_steps=completed,
        code=code,
        symbol=symbol,
        direct_cause=cause,
        causal_chain=_cycle_causal_chain(cycle, step, cause),
        impact=(
            "本轮没有生成可发送的两个主信号; 上一轮已发送信号和现有监测状态不被本次失败覆盖。"
            if workflow is FailureWorkflow.SCHEDULED_ANALYSIS
            else "本次紧急复核没有形成新方向结论, 也没有生成新阈值; 旧阈值已按一次性规则退役。"
        ),
        resolution=resolution,
        retry_action=(
            RetryAction.RERUN_SCHEDULED
            if workflow is FailureWorkflow.SCHEDULED_ANALYSIS
            else RetryAction.RERUN_EMERGENCY
        ),
        safe_diagnostics=_safe_diagnostics(
            {
                "failure_key": key,
                "candidate_symbols": cycle.candidate_symbols,
                "tracked_symbols": cycle.tracked_symbols,
                "source_analysis_id": cycle.diagnostics.get("source_analysis_id"),
                "source_scheduled_analysis_id": cycle.diagnostics.get(
                    "source_scheduled_analysis_id"
                ),
                "trigger_reasons": cycle.diagnostics.get("trigger_reasons"),
                "model": cycle.diagnostics.get("model"),
                "freshness": cycle.diagnostics.get("freshness"),
            }
        ),
    )


def monitoring_failure(
    cycle: AnalysisCycleResult,
    *,
    symbol: str | None,
    step_name: str | None,
    cause: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> FailureEvent:
    step = _monitoring_step(step_name)
    step_index = {
        FailureStep.MONITORING_REVIEW_COLLECTION: 1,
        FailureStep.MODEL_MONITORING_REVIEW: 2,
        FailureStep.MECHANICAL_ACTIVATION: 3,
    }.get(step, 3)
    clean_cause = _sanitize(cause)
    code, direct_cause = _split_failure(clean_cause, fallback="MONITORING_SETUP_FAILED")
    return FailureEvent(
        failure_id=_failure_id(
            cycle.analysis_id,
            FailureWorkflow.MONITORING_SETUP,
            step,
            symbol,
            direct_cause,
        ),
        analysis_id=cycle.analysis_id,
        occurred_at=datetime.now(UTC),
        workflow=FailureWorkflow.MONITORING_SETUP,
        step=step,
        step_index=step_index,
        total_steps=3,
        completed_steps=_completed_steps(
            step_index, FailureWorkflow.MONITORING_SETUP
        ),
        code=code,
        symbol=symbol,
        direct_cause=direct_cause,
        causal_chain=(
            f"方向信号 {cycle.analysis_id} 已先行成功保存并发送。",
            f"监测配置流程在第 {step_index}/3 步失败: {direct_cause}",
            "因此没有发布这一版本的新实时监测规则。",
        ),
        impact=(
            f"{symbol or '对应信号'} 的方向通知仍然有效, 但当前没有新阈值自动唤醒复核; "
            "失败不会删除或改写已经发送的两个主信号。"
        ),
        resolution=_resolution_for(
            code, step, FailureWorkflow.MONITORING_SETUP
        ),
        retry_action=RetryAction.RERUN_MONITORING,
        safe_diagnostics=_safe_diagnostics(
            {
                "symbol": symbol,
                "step": step_name,
                "monitoring": dict(diagnostics or {}),
            }
        ),
    )


def unexpected_failure(
    *,
    workflow: FailureWorkflow,
    analysis_id: str,
    cause: BaseException,
    symbol: str | None = None,
    safe_context: Mapping[str, Any] | None = None,
) -> FailureEvent:
    clean = _sanitize(f"{type(cause).__name__}: {cause}")
    retry_action = (
        RetryAction.RERUN_SCHEDULED
        if workflow is FailureWorkflow.SCHEDULED_ANALYSIS
        else RetryAction.RERUN_EMERGENCY
        if workflow is FailureWorkflow.EMERGENCY_ANALYSIS
        else RetryAction.RERUN_MONITORING
    )
    total = 4 if workflow is FailureWorkflow.SCHEDULED_ANALYSIS else 3
    return FailureEvent(
        failure_id=_failure_id(
            analysis_id, workflow, FailureStep.UNKNOWN, symbol, clean
        ),
        analysis_id=analysis_id,
        occurred_at=datetime.now(UTC),
        workflow=workflow,
        step=FailureStep.UNKNOWN,
        step_index=total,
        total_steps=total,
        completed_steps=(),
        code=type(cause).__name__.upper()[:120],
        symbol=symbol,
        direct_cause=clean,
        causal_chain=(
            "流程抛出了未被阶段边界转换的异常。",
            clean,
            "本次结果已停止提交, 避免用不完整状态覆盖有效结果。",
        ),
        impact=(
            "本次流程没有形成可提交的新结果; 已有成功信号保持不变。"
        ),
        resolution=(
            "按下重试按钮只重跑对应流程; 若再次失败, 查看完整诊断链定位具体提供方、模型或存储阶段。"
        ),
        retry_action=retry_action,
        safe_diagnostics=_safe_diagnostics(dict(safe_context or {})),
    )


def _failure_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _sanitize(str(key)): _sanitize(str(item))
        for key, item in value.items()
        if item is not None
    }


def _primary_failure(failures: Mapping[str, str]) -> tuple[str, str]:
    priorities = (
        "FRESHNESS.CODEX",
        "FRESHNESS_REPAIR",
        "FRESHNESS",
        "CODEX",
        "ANALYSIS",
        "SCAN",
    )
    for prefix in priorities:
        for key, value in failures.items():
            if key == prefix or key.startswith(prefix + "."):
                return key, value
    if failures:
        return next(iter(failures.items()))
    return "UNCLASSIFIED_FAILURE", "流程没有写入底层异常; 请查看完整诊断链和服务日志。"


def _cycle_failure_step(key: str, cause: str) -> FailureStep:
    upper = f"{key} {cause}".upper()
    if "FRESHNESS" in upper:
        return FailureStep.FRESHNESS_CHECK
    if "CODEX" in upper or "MODEL" in upper:
        return FailureStep.MODEL_ANALYSIS
    if "TOP-5" in upper or "EVIDENCE" in upper or "COLLECT" in upper:
        return FailureStep.EVIDENCE_COLLECTION
    if "SCAN" in upper or "CANDIDATE" in upper:
        return FailureStep.MARKET_SCAN
    return FailureStep.UNKNOWN


def _monitoring_step(value: str | None) -> FailureStep:
    try:
        step = FailureStep(value or "")
    except ValueError:
        return FailureStep.MECHANICAL_ACTIVATION
    if step in {
        FailureStep.MONITORING_REVIEW_COLLECTION,
        FailureStep.MODEL_MONITORING_REVIEW,
        FailureStep.MECHANICAL_ACTIVATION,
    }:
        return step
    return FailureStep.MECHANICAL_ACTIVATION


def _step_index(step: FailureStep, workflow: FailureWorkflow) -> int:
    if workflow is FailureWorkflow.SCHEDULED_ANALYSIS:
        return {
            FailureStep.MARKET_SCAN: 1,
            FailureStep.EVIDENCE_COLLECTION: 2,
            FailureStep.MODEL_ANALYSIS: 3,
            FailureStep.FRESHNESS_CHECK: 4,
        }.get(step, 4)
    return {
        FailureStep.EVIDENCE_COLLECTION: 1,
        FailureStep.MODEL_ANALYSIS: 2,
        FailureStep.FRESHNESS_CHECK: 3,
    }.get(step, 3)


def _completed_steps(step_index: int, workflow: FailureWorkflow) -> tuple[str, ...]:
    steps = {
        FailureWorkflow.SCHEDULED_ANALYSIS: (
            "全市场扫描与 Top5 过滤",
            "Top5 深度数据采集",
            "模型方向分析",
            "发送前新鲜度校验",
        ),
        FailureWorkflow.EMERGENCY_ANALYSIS: (
            "触发币种与市场上下文重采集",
            "模型紧急方向复核",
            "发送前新鲜度校验",
        ),
        FailureWorkflow.MONITORING_SETUP: (
            "监测复核数据重采集",
            "模型独立审核或重写阈值",
            "机械可执行性校验与版本提交",
        ),
    }[workflow]
    return steps[: max(0, step_index - 1)]


def _cycle_causal_chain(
    cycle: AnalysisCycleResult,
    step: FailureStep,
    cause: str,
) -> tuple[str, ...]:
    candidates = len(cycle.candidate_symbols)
    return (
        f"流程进入 {step.value}; 候选币种数为 {candidates}。",
        cause,
        "阶段没有产生满足契约的结果, 主流程按失败关闭并拒绝发送替补/WATCH 信号。",
    )


def _resolution_for(
    code: str,
    step: FailureStep,
    workflow: FailureWorkflow,
) -> str:
    upper = code.upper()
    if "CREDIT" in upper:
        return "为 Codex 工作区补充可用额度后, 点击按钮只重跑本次失败流程。"
    if "AUTH" in upper or "LOGIN" in upper:
        return "恢复 Codex CLI 登录/授权后, 按按钮重跑该分析流程; 无需重跑已完成的其他服务。"
    if "TIMEOUT" in upper:
        return "确认 Codex CLI 可响应后按按钮重试; 单次模型调用仍受 300 秒硬超时保护。"
    if step in {
        FailureStep.EVIDENCE_COLLECTION,
        FailureStep.MONITORING_REVIEW_COLLECTION,
        FailureStep.MARKET_SCAN,
    }:
        return "检查 Bybit/参考公开数据源连通性后按按钮重采最新数据; 不会复用已过期快照。"
    if step in {FailureStep.MODEL_ANALYSIS, FailureStep.MODEL_MONITORING_REVIEW}:
        return "按按钮重新调用模型并重新校验结构化输出; 失败的模型阶段不会用宿主规则代替。"
    if workflow is FailureWorkflow.MONITORING_SETUP:
        return "按按钮仅重跑该信号的监测复核与发布; 方向分析和通知不会重复执行。"
    return "按按钮只重跑对应流程并使用最新公开数据; 再次失败时查看完整诊断链。"


def _diagnostic_symbol(cycle: AnalysisCycleResult) -> str | None:
    symbol = cycle.diagnostics.get("symbol")
    if isinstance(symbol, str) and symbol.endswith("USDT"):
        return symbol
    if cycle.mode is CycleMode.EMERGENCY and cycle.candidate_symbols:
        return cycle.candidate_symbols[0]
    return None


def _split_failure(value: str, *, fallback: str) -> tuple[str, str]:
    clean = _sanitize(value)
    lowered = clean.lower()
    if "workspace is out of credits" in lowered or "add credits to continue" in lowered:
        return (
            "CODEX_CREDITS_EXHAUSTED",
            "Codex 工作区额度已耗尽, 服务在模型开始分析前被拒绝。",
        )
    if ":" in clean:
        prefix, rest = clean.split(":", 1)
        candidate = re.sub(r"[^A-Za-z0-9_.-]", "_", prefix.strip()).upper()
        if 2 <= len(candidate) <= 120:
            return candidate, rest.strip() or clean
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "_", fallback).upper()
    return (candidate or "UNCLASSIFIED_FAILURE")[:120], clean


def _failure_id(
    analysis_id: str,
    workflow: FailureWorkflow,
    step: FailureStep,
    symbol: str | None,
    cause: str,
) -> str:
    digest = sha256(
        "|".join((analysis_id, workflow.value, step.value, symbol or "", cause)).encode()
    ).hexdigest()[:20]
    return f"fail_{digest}"


def _sanitize(value: str) -> str:
    compact = value.replace("\r", " ").replace("\n", " ").strip()
    compact = _TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", compact)
    compact = _BOT_URL_PATTERN.sub("/bot[REDACTED]/", compact)
    return compact[:1500] or "未提供底层异常文本"


def _safe_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[TRUNCATED]"
        if isinstance(item, str):
            return _sanitize(item)
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in list(item.items())[:30]:
                lowered = str(key).lower()
                if any(secret in lowered for secret in ("token", "secret", "password", "api_key")):
                    result[str(key)] = "[REDACTED]"
                else:
                    result[str(key)] = clean(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(child, depth + 1) for child in item[:30]]
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return _sanitize(str(item))

    cleaned = clean(value)
    return cleaned if isinstance(cleaned, dict) else {}
