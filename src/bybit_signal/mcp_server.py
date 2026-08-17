from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from bybit_signal.config import AppSettings
from bybit_signal.storage.sqlite import SignalStore

mcp = MCPServer(
    "Bybit Public Signal Intelligence",
    instructions=(
        "Read-only public-market signal tools. This server has no account, position, "
        "balance, leverage or order capability. Candidate rank is not trade direction."
    ),
)


async def _store() -> SignalStore:
    config_path = Path(os.environ.get("BYBIT_SIGNAL_CONFIG", "config/system.example.yaml"))
    settings = AppSettings.from_yaml(config_path)
    database_path = settings.runtime.database_path
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    store = SignalStore(database_path)
    await store.initialize()
    return store


async def get_system_health() -> dict[str, str | int | None]:
    """Return the latest completed analysis time and strong-signal count."""

    return await (await _store()).health_summary()


async def get_latest_signals() -> dict[str, Any]:
    """Return the latest completed cycle and every current conclusion."""

    cycle = await (await _store()).latest_cycle()
    if cycle is None:
        return {"status": "EMPTY", "cycle": None}
    return {"status": "AVAILABLE", "cycle": cycle.model_dump(mode="json")}


async def get_signal_details(analysis_id: str, symbol: str) -> dict[str, Any]:
    """Return one stored conclusion including details and tool quality states."""

    conclusion = await (await _store()).conclusion(analysis_id, symbol.upper())
    if conclusion is None:
        return {"status": "NOT_FOUND", "conclusion": None}
    return {
        "status": "AVAILABLE",
        "conclusion": conclusion.model_dump(mode="json"),
    }


async def get_market_snapshot(symbol: str) -> dict[str, Any]:
    """Return the latest compact, validated evidence bundle for a symbol."""

    bundle = await (await _store()).latest_bundle(symbol.upper())
    if bundle is None:
        return {"status": "NOT_FOUND", "bundle": None}
    return {"status": "AVAILABLE", "bundle": bundle.model_dump(mode="json")}


async def list_monitoring_directives() -> dict[str, Any]:
    """List latest dynamic public-market thresholds across tracked symbols."""

    conclusions = await (await _store()).latest_conclusions()
    directives = [
        {
            "analysis_id": conclusion.analysis_id,
            "symbol": conclusion.assessment.symbol,
            "generated_at": conclusion.generated_at.isoformat(),
            "directives": [
                directive.model_dump(mode="json")
                for directive in conclusion.assessment.monitoring_directives
            ],
        }
        for conclusion in conclusions
        if conclusion.assessment.monitoring_directives
    ]
    return {"status": "AVAILABLE", "symbols": directives}


async def get_signal_history(symbol: str, limit: int = 20) -> dict[str, Any]:
    """Return up to 100 stored conclusions for one symbol, newest first."""

    history = await (await _store()).signal_history(symbol.upper(), limit=limit)
    return {
        "status": "AVAILABLE" if history else "NOT_FOUND",
        "history": [conclusion.model_dump(mode="json") for conclusion in history],
    }


mcp.tool()(get_system_health)
mcp.tool()(get_latest_signals)
mcp.tool()(get_signal_details)
mcp.tool()(get_market_snapshot)
mcp.tool()(list_monitoring_directives)
mcp.tool()(get_signal_history)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
