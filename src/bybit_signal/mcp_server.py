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
    """Return the latest successful scheduled cycle and its two primary signals."""

    cycle = await (await _store()).latest_scheduled_cycle(successful_only=True)
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
    """List active thresholds for the current scheduled primary signals."""

    conclusions = await (await _store()).active_monitoring_conclusions()
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


async def get_current_top_candidates() -> dict[str, Any]:
    """Return the latest scheduled Top-5 filter result and risk tags."""

    cycle = await (await _store()).latest_scheduled_cycle()
    if cycle is None:
        return {"status": "EMPTY", "scan": None}
    scan = cycle.diagnostics.get("scan")
    return {"status": "AVAILABLE" if scan else "NOT_FOUND", "scan": scan}


async def get_market_context() -> dict[str, Any]:
    """Return the latest stored market-wide breadth and BTC/ETH context."""

    cycle = await (await _store()).latest_scheduled_cycle(successful_only=True)
    if cycle is None or not cycle.candidate_symbols:
        return {"status": "EMPTY", "market_context": None}
    bundle = await (await _store()).latest_bundle(cycle.candidate_symbols[0])
    context = bundle.market_context if bundle is not None else None
    return {
        "status": "AVAILABLE" if context is not None else "NOT_FOUND",
        "market_context": context.model_dump(mode="json") if context else None,
    }


async def get_analysis_audit(analysis_id: str) -> dict[str, Any]:
    """Return host-managed tool calls and pre-delivery freshness checks."""

    store = await _store()
    tools = await store.tool_results(analysis_id)
    freshness = await store.freshness_checks(analysis_id)
    return {
        "status": "AVAILABLE" if tools or freshness else "NOT_FOUND",
        "tool_calls": [result.model_dump(mode="json") for result in tools],
        "freshness": [result.model_dump(mode="json") for result in freshness],
    }


async def get_threshold_events(symbol: str = "", limit: int = 50) -> dict[str, Any]:
    """Return persisted realtime threshold crossings, newest first."""

    events = await (await _store()).threshold_events(
        symbol=symbol.upper() or None,
        limit=limit,
    )
    return {
        "status": "AVAILABLE" if events else "NOT_FOUND",
        "events": list(events),
    }


async def get_signal_outcomes(symbol: str = "", limit: int = 100) -> dict[str, Any]:
    """Return automatically settled forecast, target, MFE and MAE outcomes."""

    outcomes = await (await _store()).signal_outcomes(
        symbol=symbol.upper() or None,
        limit=limit,
    )
    return {
        "status": "AVAILABLE" if outcomes else "NOT_FOUND",
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
    }


mcp.tool()(get_system_health)
mcp.tool()(get_latest_signals)
mcp.tool()(get_signal_details)
mcp.tool()(get_market_snapshot)
mcp.tool()(list_monitoring_directives)
mcp.tool()(get_signal_history)
mcp.tool()(get_current_top_candidates)
mcp.tool()(get_market_context)
mcp.tool()(get_analysis_audit)
mcp.tool()(get_threshold_events)
mcp.tool()(get_signal_outcomes)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
