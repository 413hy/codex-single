from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bybit_signal.analysis.codex import CodexAnalyzer
from bybit_signal.analysis.freshness import FreshnessGate
from bybit_signal.analysis.tool_registry import ReadOnlyAnalysisToolRegistry
from bybit_signal.config import AppSettings
from bybit_signal.domain.enums import (
    CycleMode,
    CycleStatus,
    FailureWorkflow,
    RetryAction,
)
from bybit_signal.domain.models import AnalysisCycleResult, FailureEvent
from bybit_signal.evaluation.outcomes import OutcomeEvaluator
from bybit_signal.evidence.builder import EvidenceBuilder
from bybit_signal.failures import RetrySuperseded, monitoring_failure, unexpected_failure
from bybit_signal.monitoring.engine import ThresholdEvent
from bybit_signal.monitoring.realtime import RealtimeMonitor
from bybit_signal.notifications.telegram import TelegramBot
from bybit_signal.orchestration.cycle import MonitoringStageOutcome, SignalCycleService
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.providers.cross_exchange import CrossExchangePublicClient
from bybit_signal.providers.deep_market import BybitDeepMarketCollector
from bybit_signal.providers.market_context import MarketContextCollector
from bybit_signal.providers.public_streams import PublicStreamCache
from bybit_signal.selection.scanner import BybitUniverseScanner
from bybit_signal.storage.sqlite import SignalStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeComponents:
    settings: AppSettings
    bybit: BybitPublicClient
    reference_markets: CrossExchangePublicClient | None
    store: SignalStore
    cycle_service: SignalCycleService
    telegram: TelegramBot | None
    monitor: RealtimeMonitor | None
    outcome_evaluator: OutcomeEvaluator
    stream_cache: PublicStreamCache

    async def close(self) -> None:
        if self.monitor is not None:
            await self.monitor.close()
        if self.telegram is not None:
            await self.telegram.close()
        if self.reference_markets is not None:
            await self.reference_markets.close()
        await self.bybit.close()


async def create_runtime(
    settings: AppSettings,
    *,
    workspace: Path,
) -> RuntimeComponents:
    workspace = workspace.resolve()
    store = SignalStore(_workspace_path(workspace, settings.runtime.database_path))
    await store.initialize()
    bybit = BybitPublicClient(settings.bybit.rest_base_url)
    reference_markets = (
        CrossExchangePublicClient(
            binance_base_url=settings.public_sources.binance_futures_base_url,
            okx_base_url=settings.public_sources.okx_base_url,
        )
        if settings.public_sources.cross_exchange_enabled
        else None
    )
    scanner = BybitUniverseScanner(bybit, settings.scanner)
    stream_cache = PublicStreamCache()
    market_context = MarketContextCollector(bybit, stream_cache=stream_cache)
    market_collector = BybitDeepMarketCollector(
        bybit,
        reference_client=reference_markets,
        request_concurrency=settings.runtime.market_request_concurrency,
        market_context_collector=market_context,
        stream_cache=stream_cache,
    )
    tool_registry = ReadOnlyAnalysisToolRegistry(
        bybit=bybit,
        market_context=market_context,
        store=store,
        reference_markets=reference_markets,
        timeout_seconds=settings.analysis.tool_timeout_seconds,
    )
    analyzer = CodexAnalyzer(
        settings.analysis,
        monitoring_config=settings.monitoring,
        workspace=workspace,
        runtime_root=_workspace_path(workspace, settings.runtime.data_root) / "codex",
        prompt_path=workspace / "prompts" / "signal_analysis_zh.md",
        monitoring_review_prompt_path=workspace / "prompts" / "monitoring_review_zh.md",
        model_skill_path=(
            workspace
            / ".agents"
            / "skills"
            / "analyze-bybit-ultrashort-signals"
        ),
        tool_registry=tool_registry,
    )
    cycle_service = SignalCycleService(
        settings,
        scanner=scanner,
        market_collector=market_collector,
        evidence_builder=EvidenceBuilder(),
        analyzer=analyzer,
        store=store,
        freshness_gate=(
            FreshnessGate(bybit, settings.freshness) if settings.freshness.enabled else None
        ),
    )
    telegram = TelegramBot(settings.telegram, store) if settings.telegram.enabled else None
    monitor: RealtimeMonitor | None = None
    outcome_evaluator = OutcomeEvaluator(bybit, store)

    async def on_trigger(events: tuple[ThresholdEvent, ...]) -> None:
        if telegram is not None:
            await telegram.deliver_threshold_events(events)

    async def on_emergency(
        symbol: str,
        events: tuple[ThresholdEvent, ...],
    ) -> None:
        reasons = tuple(
            f"{_threshold_condition_summary(event)}: {event.reason}"
            for event in events
        )
        try:
            result = await cycle_service.run_emergency(
                symbol,
                reasons,
                source_analysis_id=events[0].analysis_id,
            )
        except Exception as error:
            logger.exception("emergency analysis failed symbol=%s", symbol)
            if telegram is not None:
                await telegram.deliver_failure_event(
                    unexpected_failure(
                        workflow=FailureWorkflow.EMERGENCY_ANALYSIS,
                        analysis_id=_runtime_failure_analysis_id("urgent"),
                        cause=error,
                        symbol=symbol,
                        safe_context={
                            "source_analysis_id": events[0].analysis_id,
                            "trigger_reasons": reasons,
                        },
                    )
                )
            return
        if telegram is not None:
            try:
                await telegram.deliver_cycle(result)
            except Exception:
                logger.exception(
                    "emergency Telegram delivery failed analysis_id=%s",
                    result.analysis_id,
                )
        if result.status is CycleStatus.SUCCESS:
            try:
                monitoring_outcome = await cycle_service.review_and_activate_monitoring(
                    result
                )
            except Exception as error:
                logger.exception(
                    "emergency monitoring stage crashed analysis_id=%s",
                    result.analysis_id,
                )
                if telegram is not None:
                    await telegram.deliver_failure_event(
                        _unexpected_monitoring_failure(result, error, symbol=symbol)
                    )
                return
            if monitor is not None:
                await monitor.refresh_now()
            if monitoring_outcome.errors:
                logger.warning(
                    "emergency monitoring review unavailable analysis_id=%s errors=%s",
                    result.analysis_id,
                    monitoring_outcome.errors,
                )
                if telegram is not None:
                    await _deliver_monitoring_failures(
                        telegram, result, monitoring_outcome
                    )

    if settings.monitoring.enabled:
        monitor = RealtimeMonitor(
            websocket_url=settings.bybit.public_linear_ws_url,
            config=settings.monitoring,
            store=store,
            on_emergency=on_emergency,
            on_trigger=on_trigger,
            stream_cache=stream_cache,
        )

    async def retry_failure(event: FailureEvent) -> str | None:
        if event.retry_action is RetryAction.RERUN_SCHEDULED:
            result = await cycle_service.run_cycle()
            if telegram is not None:
                await telegram.deliver_cycle(result)
            if result.status is CycleStatus.FAILED:
                raise RuntimeError(
                    f"retried scheduled analysis returned FAILED ({result.analysis_id})"
                )
            outcome = await cycle_service.review_and_activate_monitoring(result)
            if telegram is not None and outcome.errors:
                await _deliver_monitoring_failures(telegram, result, outcome)
            if monitor is not None and _monitoring_can_resume(outcome):
                await monitor.resume_after_scheduled_success()
            return result.analysis_id

        cycle = await store.cycle(event.analysis_id)
        if event.retry_action is RetryAction.RERUN_MONITORING:
            if cycle is None or cycle.status is CycleStatus.FAILED:
                raise RetrySuperseded("原方向分析已不存在, 不能发布无来源的监测阈值。")
            await _ensure_cycle_authoritative(store, cycle)
            outcome = await cycle_service.review_and_activate_monitoring(
                cycle,
                symbols=(event.symbol,) if event.symbol is not None else None,
            )
            if outcome.status == "SUPERSEDED":
                raise RetrySuperseded("更新的分析版本已取代该监测配置。")
            if outcome.errors:
                if telegram is not None:
                    await _deliver_monitoring_failures(telegram, cycle, outcome)
                raise RuntimeError(
                    "monitoring retry failed: "
                    + "; ".join(f"{key}={value}" for key, value in outcome.errors.items())
                )
            if monitor is not None:
                await monitor.refresh_now()
            return cycle.analysis_id

        symbol = event.symbol
        if symbol is None:
            raise RuntimeError("emergency retry lacks a symbol")
        source_analysis_id = _safe_string(
            event.safe_diagnostics.get("source_analysis_id")
        )
        reasons = _safe_string_tuple(event.safe_diagnostics.get("trigger_reasons"))
        if not reasons:
            reasons = (f"人工重试失败事件 {event.failure_id}",)
        result = await cycle_service.run_emergency(
            symbol,
            reasons,
            source_analysis_id=source_analysis_id,
        )
        if telegram is not None:
            await telegram.deliver_cycle(result)
        if result.status is CycleStatus.FAILED:
            raise RuntimeError(
                f"retried emergency analysis returned FAILED ({result.analysis_id})"
            )
        outcome = await cycle_service.review_and_activate_monitoring(result)
        if telegram is not None and outcome.errors:
            await _deliver_monitoring_failures(telegram, result, outcome)
        if monitor is not None:
            await monitor.refresh_now()
        return result.analysis_id

    if telegram is not None:
        telegram.set_retry_handler(retry_failure)
    return RuntimeComponents(
        settings=settings,
        bybit=bybit,
        reference_markets=reference_markets,
        store=store,
        cycle_service=cycle_service,
        telegram=telegram,
        monitor=monitor,
        outcome_evaluator=outcome_evaluator,
        stream_cache=stream_cache,
    )


async def run_once(
    runtime: RuntimeComponents,
    *,
    notify: bool,
) -> AnalysisCycleResult:
    result = await runtime.cycle_service.run_cycle()
    try:
        await runtime.outcome_evaluator.settle_ready()
    except Exception:
        logger.exception("outcome settlement failed after manual cycle")
    if notify and runtime.telegram is not None:
        await runtime.telegram.deliver_cycle(result)
    if result.status is CycleStatus.SUCCESS:
        try:
            monitoring_outcome = await runtime.cycle_service.review_and_activate_monitoring(
                result
            )
        except Exception as error:
            logger.exception(
                "manual monitoring stage crashed analysis_id=%s",
                result.analysis_id,
            )
            if runtime.telegram is not None:
                await runtime.telegram.deliver_failure_event(
                    _unexpected_monitoring_failure(result, error)
                )
            return result
        if runtime.monitor is not None:
            await runtime.monitor.refresh_now()
        if monitoring_outcome.errors:
            logger.warning(
                "manual monitoring review unavailable analysis_id=%s errors=%s",
                result.analysis_id,
                monitoring_outcome.errors,
            )
            if runtime.telegram is not None:
                await _deliver_monitoring_failures(
                    runtime.telegram, result, monitoring_outcome
                )
    logger.info(
        "analysis cycle completed analysis_id=%s status=%s candidates=%s selected=%s notify=%s",
        result.analysis_id,
        result.status.value,
        len(result.candidate_symbols),
        result.selected_signal_count,
        notify,
    )
    return result


async def serve(runtime: RuntimeComponents) -> None:
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    logger.info("service starting model=%s", runtime.settings.analysis.model)
    try:
        if runtime.telegram is not None:
            await runtime.telegram.preflight()
            await runtime.telegram.announce_started()
            tasks.append(asyncio.create_task(runtime.telegram.poll_forever(stop_event)))
        if runtime.monitor is not None:
            # Startup immediately performs an authoritative scheduled refresh. Freeze
            # previously persisted thresholds before the WebSocket task can load them.
            await runtime.monitor.pause_for_scheduled_refresh()
            tasks.append(asyncio.create_task(runtime.monitor.run(stop_event)))
        tasks.append(asyncio.create_task(_scheduled_cycles(runtime, stop_event)))
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("service stopped")


async def _scheduled_cycles(
    runtime: RuntimeComponents,
    stop_event: asyncio.Event,
) -> None:
    first = True
    while not stop_event.is_set():
        if not first:
            scheduled_at = _next_half_hour(datetime.now(UTC))
            if await _wait_until(stop_event, _scheduled_pause_at(scheduled_at)):
                return
            if runtime.monitor is not None:
                await runtime.monitor.pause_for_scheduled_refresh()
        first = False
        try:
            logger.info("scheduled analysis cycle starting")
            result = await runtime.cycle_service.run_cycle()
            try:
                await runtime.outcome_evaluator.settle_ready()
            except Exception:
                logger.exception("scheduled outcome settlement failed")
            if runtime.telegram is not None:
                await runtime.telegram.deliver_cycle(result)
            if result.status is CycleStatus.SUCCESS:
                try:
                    monitoring_outcome = (
                        await runtime.cycle_service.review_and_activate_monitoring(result)
                    )
                except Exception as error:
                    logger.exception(
                        "scheduled monitoring stage crashed analysis_id=%s",
                        result.analysis_id,
                    )
                    if runtime.telegram is not None:
                        await runtime.telegram.deliver_failure_event(
                            _unexpected_monitoring_failure(result, error)
                        )
                    monitoring_outcome = None
                if monitoring_outcome is None:
                    continue
                if monitoring_outcome.errors:
                    logger.warning(
                        "scheduled monitoring review unavailable analysis_id=%s errors=%s",
                        result.analysis_id,
                        monitoring_outcome.errors,
                    )
                    if runtime.telegram is not None:
                        await _deliver_monitoring_failures(
                            runtime.telegram, result, monitoring_outcome
                        )
                if runtime.monitor is not None and _monitoring_can_resume(
                    monitoring_outcome
                ):
                    await runtime.monitor.resume_after_scheduled_success()
            logger.info(
                "scheduled analysis cycle completed analysis_id=%s status=%s selected=%s",
                result.analysis_id,
                result.status.value,
                result.selected_signal_count,
            )
        except Exception as error:
            logger.exception("scheduled analysis cycle failed")
            if runtime.telegram is not None:
                try:
                    await runtime.telegram.deliver_failure_event(
                        unexpected_failure(
                            workflow=FailureWorkflow.SCHEDULED_ANALYSIS,
                            analysis_id=_runtime_failure_analysis_id("scheduled"),
                            cause=error,
                        )
                    )
                except Exception:
                    logger.exception("failed to deliver scheduled cycle-error notice")


def _next_half_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    if now.minute < 30:
        return base.replace(minute=30)
    return base.replace(minute=0) + timedelta(hours=1)


def _scheduled_pause_at(scheduled_at: datetime) -> datetime:
    """Keep prior rules live until the authoritative scheduled cycle starts."""

    return scheduled_at


async def _wait_until(stop_event: asyncio.Event, target: datetime) -> bool:
    delay = (target - datetime.now(UTC)).total_seconds()
    if delay <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


async def _deliver_monitoring_failures(
    telegram: TelegramBot,
    cycle: AnalysisCycleResult,
    outcome: MonitoringStageOutcome,
) -> None:
    step_value = outcome.diagnostics.get("step")
    step_name = str(step_value) if step_value is not None else None
    for symbol, cause in outcome.errors.items():
        if symbol == "VERSION" or outcome.status == "SUPERSEDED":
            continue
        effective_step = step_name
        if effective_step is None:
            effective_step = _monitoring_failure_step(cause)
        await telegram.deliver_failure_event(
            monitoring_failure(
                cycle,
                symbol=symbol if symbol.endswith("USDT") else None,
                step_name=effective_step,
                cause=cause,
                diagnostics=outcome.diagnostics,
            )
        )


def _monitoring_failure_step(cause: str) -> str:
    lowered = cause.lower()
    if "activation repair" in lowered or "before activation" in lowered:
        return "MECHANICAL_ACTIVATION"
    if "model rejected" in lowered or "candidate contract" in lowered:
        return "MODEL_MONITORING_REVIEW"
    return "MECHANICAL_ACTIVATION"


async def _ensure_cycle_authoritative(
    store: SignalStore,
    cycle: AnalysisCycleResult,
) -> None:
    latest = await store.latest_scheduled_cycle(successful_only=True)
    if latest is None:
        raise RetrySuperseded("当前不存在权威定时分析。")
    if cycle.mode is CycleMode.SCHEDULED:
        expected: str | None = cycle.analysis_id
    else:
        expected = _safe_string(cycle.diagnostics.get("source_scheduled_analysis_id"))
    if latest.analysis_id != expected:
        raise RetrySuperseded(
            f"原流程基于 {expected or '未知周期'}, 当前权威周期已是 {latest.analysis_id}。"
        )


def _runtime_failure_analysis_id(prefix: str) -> str:
    return f"{prefix}_failed_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"


def _unexpected_monitoring_failure(
    cycle: AnalysisCycleResult,
    error: BaseException,
    *,
    symbol: str | None = None,
) -> FailureEvent:
    return unexpected_failure(
        workflow=FailureWorkflow.MONITORING_SETUP,
        analysis_id=cycle.analysis_id,
        cause=error,
        symbol=symbol,
        safe_context={"signal_status": cycle.status.value},
    )


def _monitoring_can_resume(outcome: MonitoringStageOutcome) -> bool:
    """Release a scheduled freeze only after a usable rule version committed."""

    return outcome.committed and outcome.status in {"READY", "PARTIAL"}


def _threshold_condition_summary(event: ThresholdEvent) -> str:
    conditions = event.matched_conditions
    if not conditions:
        return (
            f"{event.metric.value}={event.observed_value} "
            f"crossed {event.threshold}"
        )
    return "; ".join(
        f"{condition.metric.value}={condition.observed_value} "
        f"{condition.comparator.value} {condition.threshold}"
        for condition in conditions
    )


def _safe_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item is not None)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _workspace_path(workspace: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (workspace / value).resolve()
