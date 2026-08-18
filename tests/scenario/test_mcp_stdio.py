from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_read_only_mcp_stdio_lists_and_calls_health_tool(tmp_path: Path) -> None:
    config = tmp_path / "mcp.yaml"
    database = (tmp_path / "signals.db").as_posix()
    config.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "runtime:",
                f"  database_path: '{database}'",
                "telegram:",
                "  enabled: false",
            )
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["BYBIT_SIGNAL_CONFIG"] = str(config)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bybit_signal.mcp_server"],
        cwd=str(Path.cwd()),
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        health = await session.call_tool("get_system_health", {})

    assert names == {
        "get_system_health",
        "get_latest_signals",
        "get_signal_details",
        "get_market_snapshot",
        "list_monitoring_directives",
        "get_signal_history",
    }
    assert not any(
        forbidden in name
        for name in names
        for forbidden in ("order", "position", "balance", "leverage", "account")
    )
    assert health.is_error is False
    assert health.structured_content == {
        "last_analysis_id": None,
        "last_completed_at": None,
        "strong_signal_count": 0,
        "selected_signal_count": 0,
        "model_status": "NO_DATA",
        "last_successful_analysis_id": None,
        "last_emergency_analysis_id": None,
    }
