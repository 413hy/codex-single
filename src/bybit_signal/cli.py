import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from bybit_signal import __version__
from bybit_signal.config import AppSettings
from bybit_signal.operations import ServiceInstanceLock, configure_logging
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.providers.cross_exchange import CrossExchangePublicClient
from bybit_signal.providers.deep_market import BybitDeepMarketCollector
from bybit_signal.selection.scanner import BybitUniverseScanner
from bybit_signal.service import create_runtime, run_once, serve

app = typer.Typer(no_args_is_help=True, help="Bybit multi-source signal service")


@app.command("version")
def version() -> None:
    """Print the package version."""

    typer.echo(__version__)


@app.command("config-check")
def config_check(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Validate configuration without contacting external services."""

    settings = AppSettings.from_yaml(config)
    typer.echo(
        f"configuration valid: schema={settings.schema_version} "
        f"model={settings.analysis.model} telegram={settings.telegram.enabled}"
    )


@app.command("scan")
def scan(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
    limit: Annotated[int, typer.Option("--limit", min=1, max=10)] = 5,
) -> None:
    """Run the public Bybit universe scanner without invoking a model."""

    settings = AppSettings.from_yaml(config)

    async def run() -> str:
        async with BybitPublicClient(settings.bybit.rest_base_url) as client:
            result = await BybitUniverseScanner(client, settings.scanner).scan(limit=limit)
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)

    typer.echo(asyncio.run(run()))


@app.command("market-capture")
def market_capture(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
    symbol: Annotated[str, typer.Option("--symbol")],
) -> None:
    """Capture one self-contained Bybit evidence snapshot without invoking a model."""

    settings = AppSettings.from_yaml(config)

    async def run() -> str:
        reference = (
            CrossExchangePublicClient(
                binance_base_url=settings.public_sources.binance_futures_base_url,
                okx_base_url=settings.public_sources.okx_base_url,
            )
            if settings.public_sources.cross_exchange_enabled
            else None
        )
        try:
            async with BybitPublicClient(settings.bybit.rest_base_url) as client:
                snapshot = await BybitDeepMarketCollector(
                    client,
                    reference_client=reference,
                    request_concurrency=settings.runtime.market_request_concurrency,
                ).collect(symbol.upper())
        finally:
            if reference is not None:
                await reference.close()
        summary = {
            "symbol": snapshot.symbol,
            "generated_at": snapshot.generated_at.isoformat(),
            "last_price": str(snapshot.ticker.last_price),
            "mark_price": str(snapshot.ticker.mark_price),
            "completed_candles": {
                timeframe: len(candles)
                for timeframe, candles in snapshot.candles.items()
            },
            "orderbook_available": snapshot.orderbook is not None,
            "recent_trade_count": len(snapshot.recent_trades),
            "open_interest_points": len(snapshot.open_interest),
            "reference_exchanges": [
                ticker.exchange for ticker in snapshot.reference_tickers
            ],
            "collection_failures": snapshot.collection_failures,
            "sha256": snapshot.sha256,
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    typer.echo(asyncio.run(run()))


@app.command("run-cycle")
def run_cycle(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
    notify: Annotated[bool, typer.Option("--notify/--no-notify")] = True,
) -> None:
    """Run one complete native-data and Codex analysis cycle."""

    settings = AppSettings.from_yaml(config)

    async def run() -> str:
        runtime = await create_runtime(settings, workspace=Path.cwd())
        try:
            result = await run_once(runtime, notify=notify)
            return result.model_dump_json(indent=2)
        finally:
            await runtime.close()

    typer.echo(asyncio.run(run()))


@app.command("telegram-check")
def telegram_check(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
    send_test: Annotated[bool, typer.Option("--send-test/--no-send-test")] = False,
) -> None:
    """Validate Telegram credentials and optionally send the startup keyboard."""

    settings = AppSettings.from_yaml(config)

    async def run() -> str:
        runtime = await create_runtime(settings, workspace=Path.cwd())
        try:
            if runtime.telegram is None:
                raise ValueError("Telegram is disabled")
            identity = await runtime.telegram.preflight()
            if send_test:
                await runtime.telegram.announce_started()
            safe = {
                "id": identity.get("id"),
                "username": identity.get("username"),
                "can_join_groups": identity.get("can_join_groups"),
                "test_message_sent": send_test,
            }
            return json.dumps(safe, ensure_ascii=False, indent=2)
        finally:
            await runtime.close()

    typer.echo(asyncio.run(run()))


@app.command("serve")
def serve_command(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Run the 30-minute scheduler, Telegram bot and realtime monitor."""

    settings = AppSettings.from_yaml(config)
    workspace = Path.cwd()
    log_root = (
        settings.runtime.log_root.resolve()
        if settings.runtime.log_root.is_absolute()
        else (workspace / settings.runtime.log_root).resolve()
    )
    configure_logging(log_root)
    database_path = (
        settings.runtime.database_path.resolve()
        if settings.runtime.database_path.is_absolute()
        else (workspace / settings.runtime.database_path).resolve()
    )

    async def run() -> None:
        runtime = await create_runtime(settings, workspace=workspace)
        try:
            await serve(runtime)
        finally:
            await runtime.close()

    with ServiceInstanceLock(database_path.parent / "service.lock"):
        asyncio.run(run())
