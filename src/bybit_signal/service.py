from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bybit_signal.analysis.codex import CodexAnalyzer
from bybit_signal.config import AppSettings
from bybit_signal.domain.models import AnalysisCycleResult
from bybit_signal.evidence.builder import EvidenceBuilder
from bybit_signal.monitoring.engine import ThresholdEvent
from bybit_signal.monitoring.realtime import RealtimeMonitor
from bybit_signal.notifications.telegram import TelegramBot
from bybit_signal.orchestration.cycle import SignalCycleService
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.providers.cross_exchange import CrossExchangePublicClient
from bybit_signal.providers.deep_market import BybitDeepMarketCollector
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
    market_collector = BybitDeepMarketCollector(
        bybit,
        reference_client=reference_markets,
        request_concurrency=settings.runtime.market_request_concurrency,
    )
    analyzer = CodexAnalyzer(
        settings.analysis,
        workspace=workspace,
        runtime_root=_workspace_path(workspace, settings.runtime.data_root) / "codex",
        prompt_path=workspace / "prompts" / "signal_analysis_zh.md",
    )
    cycle_service = SignalCycleService(
        settings,
        scanner=scanner,
        market_collector=market_collector,
        evidence_builder=EvidenceBuilder(),
        analyzer=analyzer,
        store=store,
    )
    telegram = TelegramBot(settings.telegram, store) if settings.telegram.enabled else None
    monitor: RealtimeMonitor | None = None

    async def on_emergency(
        symbol: str,
        events: tuple[ThresholdEvent, ...],
    ) -> None:
        reasons = tuple(
            f"{event.metric.value}={event.observed_value} crossed {event.threshold}: {event.reason}"
            for event in events
        )
        try:
            result = await cycle_service.run_emergency(symbol, reasons)
        except Exception:
            logger.exception("emergency analysis failed symbol=%s", symbol)
            return
        if telegram is not None:
            try:
                await telegram.deliver_cycle(result)
            except Exception:
                logger.exception(
                    "emergency Telegram delivery failed analysis_id=%s",
                    result.analysis_id,
                )

    if settings.monitoring.enabled:
        monitor = RealtimeMonitor(
            websocket_url=settings.bybit.public_linear_ws_url,
            config=settings.monitoring,
            store=store,
            on_emergency=on_emergency,
        )
    return RuntimeComponents(
        settings=settings,
        bybit=bybit,
        reference_markets=reference_markets,
        store=store,
        cycle_service=cycle_service,
        telegram=telegram,
        monitor=monitor,
    )


async def run_once(
    runtime: RuntimeComponents,
    *,
    notify: bool,
) -> AnalysisCycleResult:
    result = await runtime.cycle_service.run_cycle()
    if notify and runtime.telegram is not None:
        await runtime.telegram.deliver_cycle(result)
    logger.info(
        "analysis cycle completed analysis_id=%s candidates=%s strong=%s notify=%s",
        result.analysis_id,
        len(result.candidate_symbols),
        result.strong_signal_count,
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
            delay = (_next_half_hour(datetime.now(UTC)) - datetime.now(UTC)).total_seconds()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.1, delay))
                return
            except TimeoutError:
                pass
        first = False
        try:
            logger.info("scheduled analysis cycle starting")
            result = await runtime.cycle_service.run_cycle()
            if runtime.telegram is not None:
                await runtime.telegram.deliver_cycle(result)
            logger.info(
                "scheduled analysis cycle completed analysis_id=%s strong=%s",
                result.analysis_id,
                result.strong_signal_count,
            )
        except Exception as error:
            logger.exception("scheduled analysis cycle failed")
            if runtime.telegram is not None:
                message = f"分析周期失败, 本轮未生成交易信号。\n错误类型: {type(error).__name__}"
                for chat_id in runtime.settings.telegram.allowed_chat_ids:
                    try:
                        await runtime.telegram.send_message(chat_id, message)
                    except Exception:
                        logger.exception(
                            "failed to deliver cycle-error notice chat_id=%s",
                            chat_id,
                        )


def _next_half_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    if now.minute < 30:
        return base.replace(minute=30)
    return base.replace(minute=0) + timedelta(hours=1)


def _workspace_path(workspace: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (workspace / value).resolve()
