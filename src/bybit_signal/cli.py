from pathlib import Path
from typing import Annotated

import typer

from bybit_signal import __version__
from bybit_signal.config import AppSettings

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
