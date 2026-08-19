from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from bybit_signal.config import AppSettings
from bybit_signal.service import create_runtime, run_once

BenchmarkEffort = Literal["medium", "high"]
BenchmarkCase = tuple[str, str, BenchmarkEffort]

DEFAULT_CASES: tuple[BenchmarkCase, ...] = (
    ("sol-medium", "gpt-5.6-sol", "medium"),
    ("sol-high", "gpt-5.6-sol", "high"),
    ("terra-medium", "gpt-5.6-terra", "medium"),
    ("terra-high", "gpt-5.6-terra", "high"),
)


async def benchmark_models_sequentially(
    settings: AppSettings,
    *,
    workspace: Path,
    cases: tuple[BenchmarkCase, ...] = DEFAULT_CASES,
    case_timeout_seconds: int = 900,
    rounds: int = 2,
) -> dict[str, object]:
    """Run each model through an isolated full pipeline, never sending Telegram."""

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = workspace.resolve() / "runtime" / "benchmarks" / stamp
    root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for round_number in range(1, rounds + 1):
        for base_case_name, model, effort in cases:
            case_name = f"round-{round_number:02d}-{base_case_name}"
            case_root = root / case_name
            payload = await _run_case(
                settings=settings,
                workspace=workspace,
                case_root=case_root,
                case_name=case_name,
                round_number=round_number,
                model=model,
                effort=effort,
                case_timeout_seconds=case_timeout_seconds,
            )
            results.append(payload)
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "execution": "SEQUENTIAL_FULL_PIPELINE_ISOLATED_NO_TELEGRAM",
        "rounds": rounds,
        "root": str(root),
        "results": results,
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


async def _run_case(
    *,
    settings: AppSettings,
    workspace: Path,
    case_root: Path,
    case_name: str,
    round_number: int,
    model: str,
    effort: BenchmarkEffort,
    case_timeout_seconds: int,
) -> dict[str, object]:
    case_settings = settings.model_copy(
        update={
            "runtime": settings.runtime.model_copy(
                update={
                    "mode": "shadow",
                    "data_root": case_root / "data",
                    "log_root": case_root / "logs",
                    "database_path": case_root / "state" / "signal.db",
                }
            ),
            "analysis": settings.analysis.model_copy(
                update={"model": model, "reasoning_effort": effort}
            ),
            "monitoring": settings.monitoring.model_copy(update={"enabled": False}),
            "telegram": settings.telegram.model_copy(
                update={
                    "enabled": False,
                    "delivery_enabled": False,
                    "allowed_chat_ids": frozenset(),
                    "allowed_user_ids": frozenset(),
                }
            ),
        }
    )
    runtime = await create_runtime(case_settings, workspace=workspace)
    started = datetime.now(UTC)
    clock = time.monotonic()
    try:
        cycle = await asyncio.wait_for(
            run_once(runtime, notify=False),
            timeout=case_timeout_seconds,
        )
        payload: dict[str, object] = {
            "case": case_name,
            "round": round_number,
            "model": model,
            "reasoning_effort": effort,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "wall_time_ms": round((time.monotonic() - clock) * 1000),
            "status": cycle.status.value,
            "analysis_id": cycle.analysis_id,
            "candidate_symbols": list(cycle.candidate_symbols),
            "selected_symbols": list(cycle.selected_symbols),
            "selected_signal_count": cycle.selected_signal_count,
            "diagnostics": cycle.diagnostics,
            "cycle": cycle.model_dump(mode="json"),
        }
    except TimeoutError:
        payload = {
            "case": case_name,
            "round": round_number,
            "model": model,
            "reasoning_effort": effort,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "wall_time_ms": round((time.monotonic() - clock) * 1000),
            "status": "TIMEOUT",
            "error": f"full pipeline exceeded {case_timeout_seconds}s",
        }
    except Exception as error:
        payload = {
            "case": case_name,
            "round": round_number,
            "model": model,
            "reasoning_effort": effort,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "wall_time_ms": round((time.monotonic() - clock) * 1000),
            "status": "ERROR",
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        await runtime.close()
    (case_root / "result.json").parent.mkdir(parents=True, exist_ok=True)
    (case_root / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload
