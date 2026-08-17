from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bybit_signal.cmi.adapter import CmiAdapter, CmiCaptureError, ProcessResult
from bybit_signal.config import CmiConfig
from bybit_signal.domain.enums import ToolStatus


def _config(tmp_path: Path) -> CmiConfig:
    source_root = tmp_path / "source"
    (source_root / "app").mkdir(parents=True)
    (source_root / "app" / "__main__.py").write_text("", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    return CmiConfig(
        enabled=True,
        mode="source",
        source_root=source_root,
        python_executable=python,
        live_seconds=5,
        timeout_seconds=15,
    )


def _payload(
    symbol: str,
    *,
    generated_at: datetime | None = None,
    bybit_symbol: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(UTC)
    identity = bybit_symbol or symbol
    return {
        "schema_version": "2.0",
        "generated_at": generated_at.isoformat(),
        "snapshot_status": "USABLE_WITH_PARTIAL_WINDOWS",
        "application": {"application_version": "2.2.1"},
        "instrument": {
            "bybit:perp": {
                "normalized_symbol": identity,
                "symbol_status": "Trading",
            },
            "binance:perp": {
                "normalized_symbol": symbol,
                "symbol_status": "TRADING",
            },
        },
        "perpetual_tickers": {
            "bybit": {
                "normalized_symbol": identity,
                "last_price": 0.5,
                "mark_price": 0.501,
                "is_stale": False,
            }
        },
        "health": {
            "snapshot_completeness_status": "INCOMPLETE",
            "overall_status": "DEGRADED",
        },
        "data_limitations": [
            {
                "code": "NO_COMPLETED_CANDLES",
                "exchange": "okx",
                "market": "perp",
                "message": "no completed candles",
            }
        ],
    }


class FakeCmiRunner:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        capture_return_code: int = 0,
        escape_output: bool = False,
    ) -> None:
        self.payload = payload or _payload("CYSUSDT")
        self.capture_return_code = capture_return_code
        self.escape_output = escape_output
        self.commands: list[tuple[str, ...]] = []

    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        command_tuple = tuple(command)
        self.commands.append(command_tuple)
        if "-c" in command_tuple:
            return ProcessResult(0, "CMI_PREFLIGHT_OK\n", "", 1)
        if self.capture_return_code:
            return ProcessResult(self.capture_return_code, "", "capture failed", 2)
        root = Path(command_tuple[command_tuple.index("--data-root") + 1])
        symbol = command_tuple[command_tuple.index("--profile") + 1]
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        json_path = output / f"{symbol}_latest.json"
        text_path = output / f"{symbol}_latest.txt"
        health_path = output / f"{symbol}_health.json"
        json_path.write_text(json.dumps(self.payload), encoding="utf-8")
        text_path.write_text("CMI snapshot", encoding="utf-8")
        health_path.write_text("{}", encoding="utf-8")
        reported_json = root.parent / "escape.json" if self.escape_output else json_path
        event = {
            "stage": "COMPLETED",
            "files": {
                "latest_json": str(reported_json),
                "latest_text": str(text_path),
                "health_json": str(health_path),
            },
        }
        return ProcessResult(0, json.dumps(event) + "\n", "", 3)


async def test_capture_accepts_new_partial_multi_exchange_snapshot(tmp_path: Path) -> None:
    runner = FakeCmiRunner()
    adapter = CmiAdapter(_config(tmp_path), tmp_path / "data", process_runner=runner)

    snapshot = await adapter.capture("cysusdt")

    assert snapshot.symbol == "CYSUSDT"
    assert snapshot.status is ToolStatus.PARTIAL
    assert snapshot.available_perpetual_exchanges == ("binance", "bybit")
    assert snapshot.limitation_count == 1
    assert snapshot.payload["perpetual_tickers"]["bybit"]["last_price"] == 0.5
    assert len(runner.commands) == 2
    assert runner.commands[1][1:3] == ("-m", "app")


async def test_capture_rejects_wrong_instrument_identity(tmp_path: Path) -> None:
    runner = FakeCmiRunner(payload=_payload("CYSUSDT", bybit_symbol="GPSUSDT"))
    adapter = CmiAdapter(_config(tmp_path), tmp_path / "data", process_runner=runner)

    with pytest.raises(CmiCaptureError) as captured:
        await adapter.capture("CYSUSDT")

    assert captured.value.code == "SYMBOL_IDENTITY_MISMATCH"


async def test_failed_capture_never_reuses_previous_latest_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    data_root = tmp_path / "data"
    old = data_root / "CYSUSDT" / "output" / "CYSUSDT_latest.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(_payload("CYSUSDT")), encoding="utf-8")
    runner = FakeCmiRunner(capture_return_code=4)
    adapter = CmiAdapter(config, data_root, process_runner=runner)

    with pytest.raises(CmiCaptureError) as captured:
        await adapter.capture("CYSUSDT")

    assert captured.value.code == "CAPTURE_PROCESS_FAILED"
    assert old.is_file()


async def test_capture_rejects_snapshot_from_before_invocation(tmp_path: Path) -> None:
    old_time = datetime.now(UTC) - timedelta(minutes=5)
    runner = FakeCmiRunner(payload=_payload("CYSUSDT", generated_at=old_time))
    adapter = CmiAdapter(_config(tmp_path), tmp_path / "data", process_runner=runner)

    with pytest.raises(CmiCaptureError) as captured:
        await adapter.capture("CYSUSDT")

    assert captured.value.code == "STALE_SNAPSHOT"


async def test_capture_rejects_reported_path_outside_symbol_root(tmp_path: Path) -> None:
    runner = FakeCmiRunner(escape_output=True)
    adapter = CmiAdapter(_config(tmp_path), tmp_path / "data", process_runner=runner)

    with pytest.raises(CmiCaptureError) as captured:
        await adapter.capture("CYSUSDT")

    assert captured.value.code == "UNSAFE_OUTPUT_PATH"


async def test_capture_rejects_invalid_symbol_before_process_start(tmp_path: Path) -> None:
    runner = FakeCmiRunner()
    adapter = CmiAdapter(_config(tmp_path), tmp_path / "data", process_runner=runner)

    with pytest.raises(CmiCaptureError) as captured:
        await adapter.capture("CYS/USDT")

    assert captured.value.code == "INVALID_SYMBOL"
    assert runner.commands == []
