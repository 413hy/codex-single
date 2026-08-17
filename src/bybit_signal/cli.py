import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from bybit_signal import __version__
from bybit_signal.config import AppSettings
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.selection.scanner import BybitUniverseScanner

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
