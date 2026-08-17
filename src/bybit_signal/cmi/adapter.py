from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from bybit_signal.cmi.models import CmiSnapshot
from bybit_signal.config import CmiConfig
from bybit_signal.domain.enums import ToolStatus


class CmiCaptureError(RuntimeError):
    """Raised when CMI cannot produce a new trustworthy snapshot."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    return_code: int
    stdout: str
    stderr: str
    latency_ms: int


class ProcessRunner(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessResult: ...


async def run_process(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
) -> ProcessResult:
    started = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise CmiCaptureError(
            "PROCESS_TIMEOUT",
            f"CMI process exceeded {timeout_seconds}s",
        ) from error
    return ProcessResult(
        return_code=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        latency_ms=round((time.monotonic() - started) * 1000),
    )


class CmiAdapter:
    def __init__(
        self,
        config: CmiConfig,
        data_root: Path,
        *,
        max_parallel: int = 3,
        process_runner: ProcessRunner = run_process,
    ) -> None:
        if not config.enabled:
            raise ValueError("CMI adapter cannot be created when CMI is disabled")
        self._config = config
        self._data_root = data_root.resolve()
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._process_runner = process_runner
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._preflight_lock = asyncio.Lock()
        self._preflight_complete = False

    async def preflight(self) -> None:
        if self._preflight_complete:
            return
        async with self._preflight_lock:
            if self._preflight_complete:
                return
            if self._config.mode == "source":
                await self._preflight_source()
            else:
                executable = cast(Path, self._config.executable_path).resolve()
                if not executable.is_file():
                    raise CmiCaptureError(
                        "PREFLIGHT_EXECUTABLE_MISSING",
                        f"CMI executable does not exist: {executable}",
                    )
            self._preflight_complete = True

    async def capture(self, symbol: str) -> CmiSnapshot:
        normalized_symbol = symbol.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,24}USDT", normalized_symbol) is None:
            raise CmiCaptureError("INVALID_SYMBOL", "CMI symbol must be a USDT instrument")
        await self.preflight()
        symbol_lock = self._symbol_locks.setdefault(normalized_symbol, asyncio.Lock())
        async with self._semaphore, symbol_lock:
            return await self._capture_locked(normalized_symbol)

    async def _preflight_source(self) -> None:
        source_root = cast(Path, self._config.source_root).resolve()
        python_executable = cast(Path, self._config.python_executable).resolve()
        if not source_root.is_dir():
            raise CmiCaptureError(
                "PREFLIGHT_SOURCE_MISSING",
                f"CMI source root does not exist: {source_root}",
            )
        if not (source_root / "app" / "__main__.py").is_file():
            raise CmiCaptureError(
                "PREFLIGHT_ENTRYPOINT_MISSING",
                f"CMI source entry point is missing under: {source_root}",
            )
        if not python_executable.is_file():
            raise CmiCaptureError(
                "PREFLIGHT_PYTHON_MISSING",
                f"CMI Python executable does not exist: {python_executable}",
            )
        result = await self._process_runner(
            (
                str(python_executable),
                "-c",
                "import app, pyarrow; print('CMI_PREFLIGHT_OK')",
            ),
            source_root,
            min(30, self._config.timeout_seconds),
        )
        if result.return_code != 0 or "CMI_PREFLIGHT_OK" not in result.stdout:
            raise CmiCaptureError(
                "PREFLIGHT_IMPORT_FAILED",
                self._process_failure("CMI source dependency preflight failed", result),
            )

    async def _capture_locked(self, symbol: str) -> CmiSnapshot:
        symbol_root = (self._data_root / symbol).resolve()
        self._require_contained(symbol_root, self._data_root, "symbol data root")
        symbol_root.mkdir(parents=True, exist_ok=True)
        expected_json = symbol_root / "output" / f"{symbol}_latest.json"
        previous = self._file_identity(expected_json)
        started_at = datetime.now(UTC)
        command, cwd = self._capture_command(symbol, symbol_root)
        result = await self._process_runner(
            command,
            cwd,
            self._config.timeout_seconds + 15,
        )
        if result.return_code != 0:
            raise CmiCaptureError(
                "CAPTURE_PROCESS_FAILED",
                self._process_failure(f"CMI capture failed for {symbol}", result),
            )
        completed = self._completed_event(result.stdout)
        files = completed.get("files")
        file_map = files if isinstance(files, Mapping) else {}
        json_path = self._event_path(file_map.get("latest_json"), expected_json, symbol_root)
        text_path = self._event_path(
            file_map.get("latest_text"),
            symbol_root / "output" / f"{symbol}_latest.txt",
            symbol_root,
        )
        health_path = self._event_path(
            file_map.get("health_json"),
            symbol_root / "output" / f"{symbol}_health.json",
            symbol_root,
        )
        for path in (json_path, text_path, health_path):
            if not path.is_file():
                raise CmiCaptureError(
                    "CAPTURE_OUTPUT_MISSING",
                    f"CMI reported completion but output is missing: {path.name}",
                )
        current = self._file_identity(json_path)
        if current is None:
            raise CmiCaptureError(
                "CAPTURE_OUTPUT_MISSING",
                f"CMI snapshot disappeared before validation: {json_path.name}",
            )
        if previous is not None and current == previous:
            raise CmiCaptureError(
                "CAPTURE_OUTPUT_UNCHANGED",
                f"CMI did not replace the previous {symbol} snapshot",
            )
        payload = self._load_payload(json_path)
        return self._validate_payload(
            symbol=symbol,
            payload=payload,
            started_at=started_at,
            json_path=json_path,
            text_path=text_path,
            health_path=health_path,
            sha256=current[0],
        )

    def _capture_command(self, symbol: str, symbol_root: Path) -> tuple[tuple[str, ...], Path]:
        arguments = (
            "--profile",
            symbol,
            "--data-root",
            str(symbol_root),
            "capture-once",
            "--live-seconds",
            str(self._config.live_seconds),
            "--timeout-seconds",
            str(self._config.timeout_seconds),
            "--progress-json",
        )
        if self._config.mode == "source":
            return (
                str(cast(Path, self._config.python_executable).resolve()),
                "-m",
                "app",
                *arguments,
            ), cast(Path, self._config.source_root).resolve()
        executable = cast(Path, self._config.executable_path).resolve()
        return (str(executable), *arguments), executable.parent

    def _validate_payload(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        started_at: datetime,
        json_path: Path,
        text_path: Path,
        health_path: Path,
        sha256: str,
    ) -> CmiSnapshot:
        if payload.get("schema_version") != "2.0":
            raise CmiCaptureError("SCHEMA_MISMATCH", "CMI schema_version must be 2.0")
        generated_at = self._parse_datetime(payload.get("generated_at"), "generated_at")
        now = datetime.now(UTC)
        if generated_at < started_at - timedelta(seconds=5):
            raise CmiCaptureError(
                "STALE_SNAPSHOT",
                "CMI output predates the current capture invocation",
            )
        age_seconds = (now - generated_at).total_seconds()
        if age_seconds > self._config.max_snapshot_age_seconds:
            raise CmiCaptureError(
                "STALE_SNAPSHOT",
                f"CMI snapshot is {age_seconds:.1f}s old",
            )
        if age_seconds < -30:
            raise CmiCaptureError("FUTURE_SNAPSHOT", "CMI snapshot timestamp is in the future")

        instrument = self._mapping(payload.get("instrument"), "instrument")
        bybit_instrument = self._mapping(instrument.get("bybit:perp"), "instrument.bybit:perp")
        self._require_symbol(bybit_instrument, symbol, "instrument.bybit:perp")
        if bybit_instrument.get("symbol_status") != "Trading":
            raise CmiCaptureError("BYBIT_NOT_TRADING", f"{symbol} is not Trading on Bybit")

        perpetual_tickers = self._mapping(payload.get("perpetual_tickers"), "perpetual_tickers")
        bybit_ticker = self._mapping(perpetual_tickers.get("bybit"), "perpetual_tickers.bybit")
        self._require_symbol(bybit_ticker, symbol, "perpetual_tickers.bybit")
        for field in ("last_price", "mark_price"):
            value = bybit_ticker.get(field)
            if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                raise CmiCaptureError(
                    "BYBIT_CORE_EVIDENCE_MISSING",
                    f"CMI Bybit ticker has no valid {field}",
                )
        if bybit_ticker.get("is_stale") is True:
            raise CmiCaptureError("BYBIT_CORE_EVIDENCE_STALE", "CMI Bybit ticker is stale")

        exchanges = tuple(
            sorted(
                key.removesuffix(":perp")
                for key, value in instrument.items()
                if key.endswith(":perp") and isinstance(value, Mapping)
            )
        )
        limitations = payload.get("data_limitations")
        limitation_items = limitations if isinstance(limitations, list) else []
        limitation_summaries = tuple(
            self._limitation_summary(item)
            for item in limitation_items[:20]
            if isinstance(item, Mapping)
        )
        health = self._mapping(payload.get("health"), "health")
        completeness = str(health.get("snapshot_completeness_status") or "UNKNOWN")
        health_status = str(health.get("overall_status") or "UNKNOWN")
        snapshot_status = str(payload.get("snapshot_status") or "UNKNOWN")
        partial = (
            "PARTIAL" in snapshot_status
            or completeness != "COMPLETE"
            or health_status != "HEALTHY"
        )
        application = self._mapping(payload.get("application"), "application")
        version = str(application.get("application_version") or "UNKNOWN")
        return CmiSnapshot(
            symbol=symbol,
            schema_version="2.0",
            generated_at=generated_at,
            captured_at=datetime.now(UTC),
            json_path=json_path,
            text_path=text_path,
            health_path=health_path,
            sha256=sha256,
            status=ToolStatus.PARTIAL if partial else ToolStatus.AVAILABLE,
            snapshot_status=snapshot_status,
            completeness_status=completeness,
            health_status=health_status,
            application_version=version,
            available_perpetual_exchanges=exchanges,
            limitation_count=len(limitation_items),
            limitation_summaries=limitation_summaries,
            payload=payload,
        )

    def _load_payload(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        if size <= 0 or size > self._config.max_snapshot_bytes:
            raise CmiCaptureError(
                "SNAPSHOT_SIZE_INVALID",
                f"CMI snapshot size {size} is outside the configured boundary",
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CmiCaptureError(
                "SNAPSHOT_JSON_INVALID",
                "CMI snapshot is not valid UTF-8 JSON",
            ) from error
        if not isinstance(value, dict):
            raise CmiCaptureError("SNAPSHOT_ROOT_INVALID", "CMI snapshot root must be an object")
        return value

    @staticmethod
    def _completed_event(stdout: str) -> dict[str, Any]:
        completed: dict[str, Any] | None = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("stage") == "COMPLETED":
                completed = event
        if completed is None:
            raise CmiCaptureError(
                "CAPTURE_COMPLETION_MISSING",
                "CMI exited successfully without a COMPLETED progress event",
            )
        return completed

    @staticmethod
    def _mapping(value: object, field: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise CmiCaptureError("SNAPSHOT_FIELD_INVALID", f"CMI {field} must be an object")
        return cast(Mapping[str, Any], value)

    @staticmethod
    def _parse_datetime(value: object, field: str) -> datetime:
        if not isinstance(value, str):
            raise CmiCaptureError("SNAPSHOT_TIME_INVALID", f"CMI {field} must be a timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise CmiCaptureError("SNAPSHOT_TIME_INVALID", f"CMI {field} is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CmiCaptureError("SNAPSHOT_TIME_INVALID", f"CMI {field} must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _require_symbol(value: Mapping[str, Any], symbol: str, field: str) -> None:
        normalized = value.get("normalized_symbol")
        if normalized != symbol:
            raise CmiCaptureError(
                "SYMBOL_IDENTITY_MISMATCH",
                f"CMI {field} identity {normalized!r} does not match {symbol}",
            )

    @staticmethod
    def _limitation_summary(item: Mapping[str, Any]) -> str:
        parts = (
            str(item.get("code") or "UNKNOWN"),
            str(item.get("exchange") or "all"),
            str(item.get("market") or "all"),
            str(item.get("message") or "no message"),
        )
        return " | ".join(parts)[:500]

    @staticmethod
    def _file_identity(path: Path) -> tuple[str, int, int] | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        return digest.hexdigest(), stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _event_path(value: object, fallback: Path, root: Path) -> Path:
        path = Path(value) if isinstance(value, str) and value else fallback
        if not path.is_absolute():
            path = root / path
        resolved = path.resolve()
        CmiAdapter._require_contained(resolved, root, "CMI output")
        return resolved

    @staticmethod
    def _require_contained(path: Path, root: Path, label: str) -> None:
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise CmiCaptureError(
                "UNSAFE_OUTPUT_PATH",
                f"{label} escapes its configured data root",
            ) from error

    @staticmethod
    def _process_failure(prefix: str, result: ProcessResult) -> str:
        detail = (result.stderr or result.stdout).strip().replace("\r", " ").replace("\n", " ")
        if len(detail) > 600:
            detail = detail[:597] + "..."
        return f"{prefix} (exit={result.return_code}): {detail or 'no diagnostic output'}"
