from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from bybit_signal.config import AnalysisConfig
from bybit_signal.domain.enums import Direction, MonitoringMetric, SignalStrength
from bybit_signal.domain.models import (
    CandidateAssessment,
    EvidenceBundle,
    ModelAnalysisResponse,
)


class CodexAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CodexProcessResult:
    return_code: int
    stdout: str
    stderr: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class CodexAnalysisResult:
    response: ModelAnalysisResponse
    context_sha256: str
    latency_ms: int
    attempts: int
    usage: dict[str, int]


class CodexProcessRunner(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        stdin: str,
    ) -> CodexProcessResult: ...


async def run_codex_process(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    stdin: str,
) -> CodexProcessResult:
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    last_start_error: OSError | None = None
    for attempt in range(1, 4):
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            break
        except OSError as error:
            last_start_error = error
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)
    if process is None:
        raise CodexAnalysisError(
            "CODEX_START_FAILED",
            "Codex process could not be started after three attempts",
        ) from last_start_error
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode("utf-8")),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        process.kill()
        await process.communicate()
        raise CodexAnalysisError(
            "CODEX_TIMEOUT",
            f"Codex analysis exceeded {timeout_seconds}s",
        ) from error
    return CodexProcessResult(
        return_code=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        latency_ms=round((time.monotonic() - started) * 1000),
    )


class CodexAnalyzer:
    def __init__(
        self,
        config: AnalysisConfig,
        *,
        workspace: Path,
        runtime_root: Path,
        prompt_path: Path,
        process_runner: CodexProcessRunner = run_codex_process,
        codex_executable: str = "codex",
    ) -> None:
        self._config = config
        self._workspace = workspace.resolve()
        self._runtime_root = runtime_root.resolve()
        self._prompt_path = prompt_path.resolve()
        self._process_runner = process_runner
        self._codex_command_prefix = _codex_command_prefix(codex_executable)

    async def analyze(
        self,
        *,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        tracked_symbols: Sequence[str] = (),
    ) -> CodexAnalysisResult:
        if not bundles:
            raise CodexAnalysisError("EMPTY_CONTEXT", "Codex analysis requires evidence")
        if not self._prompt_path.is_file():
            raise CodexAnalysisError(
                "PROMPT_MISSING",
                f"analysis prompt is missing: {self._prompt_path}",
            )
        symbols = [bundle.symbol for bundle in bundles]
        if len(symbols) != len(set(symbols)):
            raise CodexAnalysisError("DUPLICATE_SYMBOL", "analysis bundles contain duplicates")
        requested_at = datetime.now(UTC)
        forming_window_start = requested_at.replace(minute=0, second=0, microsecond=0)
        forming_window_end = forming_window_start + timedelta(hours=1)
        context = self._context(
            analysis_id=analysis_id,
            bundles=bundles,
            tracked_symbols=tracked_symbols,
            max_strong_signals=2,
            max_target_distance_percent=self._config.max_target_distance_percent,
            requested_at=requested_at,
        )
        context_json = json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        base_prompt = self._prompt_path.read_text(encoding="utf-8")
        validation_feedback = ""
        total_latency = 0
        last_error = "no model attempt completed"
        usage: dict[str, int] = {}
        self._runtime_root.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, self._config.max_attempts + 1):
            prompt = base_prompt + "\n\n# 本轮输入\n" + context_json + validation_feedback
            with tempfile.TemporaryDirectory(
                prefix="codex-analysis-",
                dir=self._runtime_root,
            ) as temporary:
                temporary_root = Path(temporary)
                schema_path = temporary_root / "response.schema.json"
                output_path = temporary_root / "last-message.json"
                schema = ModelAnalysisResponse.model_json_schema()
                _strict_output_schema(schema)
                schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False),
                    encoding="utf-8",
                )
                command = self._command(schema_path, output_path)
                process = await self._process_runner(
                    command,
                    self._workspace,
                    self._config.timeout_seconds,
                    prompt,
                )
                total_latency += process.latency_ms
                usage = self._usage(process.stdout) or usage
                if process.return_code != 0:
                    last_error = self._process_failure(process)
                    validation_feedback = self._feedback(last_error)
                    continue
                if not output_path.is_file():
                    last_error = "Codex did not create its last-message output"
                    validation_feedback = self._feedback(last_error)
                    continue
                if output_path.stat().st_size > 2 * 1024 * 1024:
                    last_error = "Codex last-message output exceeds 2 MiB"
                    validation_feedback = self._feedback(last_error)
                    continue
                try:
                    response = ModelAnalysisResponse.model_validate_json(
                        output_path.read_text(encoding="utf-8")
                    )
                    self._validate_response(
                        response=response,
                        analysis_id=analysis_id,
                        bundles=bundles,
                        forming_window_start=forming_window_start,
                        forming_window_end=forming_window_end,
                    )
                except (OSError, UnicodeError, ValidationError, ValueError) as error:
                    last_error = self._concise_error(error)
                    validation_feedback = self._feedback(last_error)
                    continue
                return CodexAnalysisResult(
                    response=response,
                    context_sha256=context_sha256,
                    latency_ms=total_latency,
                    attempts=attempt,
                    usage=usage,
                )
        raise CodexAnalysisError(
            "INVALID_MODEL_OUTPUT",
            f"Codex failed after {self._config.max_attempts} attempt(s): {last_error}",
        )

    def _command(self, schema_path: Path, output_path: Path) -> tuple[str, ...]:
        return (
            *self._codex_command_prefix,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--model",
            self._config.model,
            "-c",
            f'model_reasoning_effort="{self._config.reasoning_effort}"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--cd",
            str(self._workspace),
            "-",
        )

    @staticmethod
    def _context(
        *,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        tracked_symbols: Sequence[str],
        max_strong_signals: int,
        max_target_distance_percent: float,
        requested_at: datetime,
    ) -> dict[str, Any]:
        window_start = requested_at.replace(minute=0, second=0, microsecond=0)
        cutoffs = [bundle.generated_at for bundle in bundles]
        return {
            "schema_version": 1,
            "analysis_id": analysis_id,
            "requested_at": requested_at.isoformat(),
            "batch_evidence_cutoff_utc": max(cutoffs).isoformat(),
            "bundle_cutoff_skew_seconds": (
                max(cutoffs) - min(cutoffs)
            ).total_seconds(),
            "forming_1h_window_utc": {
                "start": window_start.isoformat(),
                "end": (window_start + timedelta(hours=1)).isoformat(),
            },
            "analysis_policy": {
                "max_strong_signals": max_strong_signals,
                "max_target_distance_percent": max_target_distance_percent,
                "monitoring_valid_for_seconds": {"minimum": 1800, "maximum": 3600},
            },
            "tracked_symbols_without_prior_direction": sorted(set(tracked_symbols)),
            "evidence_bundles": [bundle.model_dump(mode="json") for bundle in bundles],
        }

    def _validate_response(
        self,
        *,
        response: ModelAnalysisResponse,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        forming_window_start: datetime,
        forming_window_end: datetime,
    ) -> None:
        if response.analysis_id != analysis_id:
            raise ValueError("analysis_id does not match the request")
        expected = {bundle.symbol for bundle in bundles}
        actual = {assessment.symbol for assessment in response.assessments}
        if actual != expected:
            raise ValueError(
                f"assessment symbols differ: expected {sorted(expected)}, got {sorted(actual)}"
            )
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        for assessment in response.assessments:
            bundle = bundle_by_symbol[assessment.symbol]
            known = {item.evidence_id for item in bundle.evidence_items}
            referenced = set(assessment.evidence_ids)
            if assessment.forming_1h is not None and (
                assessment.forming_1h.window_start != forming_window_start
                or assessment.forming_1h.window_end != forming_window_end
            ):
                raise ValueError(
                    f"{assessment.symbol} forming 1h window differs from the requested window"
                )
            if assessment.take_profit is not None:
                referenced.update(assessment.take_profit.evidence_ids)
            if assessment.invalidation is not None:
                referenced.update(assessment.invalidation.evidence_ids)
            family_ids: set[str] = set()
            for directive in assessment.monitoring_directives:
                referenced.update(directive.evidence_ids)
                if directive.family_id in family_ids:
                    raise ValueError(
                        f"{assessment.symbol} contains duplicate monitoring family "
                        f"{directive.family_id}"
                    )
                family_ids.add(directive.family_id)
                if not 1800 <= directive.valid_for_seconds <= 3600:
                    raise ValueError(
                        f"{assessment.symbol} monitoring directive validity must be "
                        "between 1800 and 3600 seconds"
                    )
                if directive.metric not in {
                    MonitoringMetric.LAST_PRICE,
                    MonitoringMetric.MARK_PRICE,
                    MonitoringMetric.COMPLETED_5M_CLOSE,
                    MonitoringMetric.COMPLETED_15M_CLOSE,
                    MonitoringMetric.COMPLETED_1H_CLOSE,
                    MonitoringMetric.SPREAD_BPS,
                    MonitoringMetric.OPEN_INTEREST,
                    MonitoringMetric.FUNDING_RATE,
                }:
                    raise ValueError(
                        f"{assessment.symbol} requested unsupported realtime metric "
                        f"{directive.metric.value}"
                    )
            if not referenced <= known:
                unknown = sorted(referenced - known)
                raise ValueError(f"{assessment.symbol} references unknown evidence: {unknown}")
            self._validate_direction_consistency(assessment, bundle)
            if assessment.strength is SignalStrength.STRONG:
                self._validate_strong_price_geometry(assessment, bundle)

    def _validate_direction_consistency(
        self,
        assessment: CandidateAssessment,
        bundle: EvidenceBundle,
    ) -> None:
        direction = assessment.direction
        if direction is None:
            return
        five = _evidence_values(bundle, ".PA.5M")
        fifteen = _evidence_values(bundle, ".PA.15M")
        if five is None or fifteen is None:
            return

        five_return = _number(five, "return_3_percent")
        fifteen_return = _number(fifteen, "return_3_percent")
        fifteen_atr_percent = _number(fifteen, "atr_14_percent")
        if five_return is None or fifteen_return is None or fifteen_atr_percent is None:
            return

        impulse_threshold = max(12.0, 3 * fifteen_atr_percent)
        drawdown = _atr_distance(five, high=True)
        rebound = _atr_distance(five, high=False)
        pivot_high_age = _pivot_age_bars(five, high=True, timeframe_minutes=5)
        pivot_low_age = _pivot_age_bars(five, high=False, timeframe_minutes=5)

        upward_exhaustion = (
            fifteen_return >= impulse_threshold
            and five_return <= 0
            and drawdown is not None
            and drawdown >= 1.5
            and pivot_high_age is not None
            and pivot_high_age <= 3
        )
        downward_exhaustion = (
            fifteen_return <= -impulse_threshold
            and five_return >= 0
            and rebound is not None
            and rebound >= 1.5
            and pivot_low_age is not None
            and pivot_low_age <= 3
        )
        if direction is Direction.LONG_BIAS and upward_exhaustion:
            raise ValueError(
                f"{assessment.symbol} LONG_BIAS conflicts with completed-candle "
                "upward exhaustion: 15m impulse, confirmed 5m pivot high, "
                "negative 5m return and >=1.5 ATR drawdown"
            )
        if direction is Direction.SHORT_BIAS and downward_exhaustion:
            raise ValueError(
                f"{assessment.symbol} SHORT_BIAS conflicts with completed-candle "
                "downward exhaustion: 15m impulse, confirmed 5m pivot low, "
                "positive 5m return and >=1.5 ATR rebound"
            )

        fifteen_close = _number(fifteen, "latest_close")
        fifteen_mid = _number(fifteen, "range_mid_20")
        five_macd = _number(five, "macd_histogram_12_26_9")
        five_efficiency = _number(five, "directional_efficiency_12")
        if (
            fifteen_close is None
            or fifteen_mid is None
            or five_macd is None
            or five_efficiency is None
        ):
            return
        confirmed_downswing = (
            not downward_exhaustion
            and fifteen_return <= -1.5 * fifteen_atr_percent
            and fifteen_close < fifteen_mid
            and five_macd < 0
            and five_efficiency < 0
        )
        confirmed_upswing = (
            not upward_exhaustion
            and fifteen_return >= 1.5 * fifteen_atr_percent
            and fifteen_close > fifteen_mid
            and five_macd > 0
            and five_efficiency > 0
        )
        if direction is Direction.LONG_BIAS and confirmed_downswing:
            raise ValueError(
                f"{assessment.symbol} LONG_BIAS conflicts with a confirmed near-term "
                "downswing across completed 15m structure and 5m momentum"
            )
        if direction is Direction.SHORT_BIAS and confirmed_upswing:
            raise ValueError(
                f"{assessment.symbol} SHORT_BIAS conflicts with a confirmed near-term "
                "upswing across completed 15m structure and 5m momentum"
            )

    def _validate_strong_price_geometry(
        self,
        assessment: CandidateAssessment,
        bundle: EvidenceBundle,
    ) -> None:
        if assessment.take_profit is None or assessment.invalidation is None:
            raise ValueError(f"{assessment.symbol} strong signal lacks target or invalidation")
        current = bundle.canonical_last.value
        target = assessment.take_profit.value
        invalidation = assessment.invalidation.reference_price
        if assessment.direction is Direction.LONG_BIAS:
            if target <= current or invalidation >= current:
                raise ValueError(
                    f"{assessment.symbol} long target/invalidation geometry is invalid"
                )
        elif assessment.direction is Direction.SHORT_BIAS and (
            target >= current or invalidation <= current
        ):
            raise ValueError(f"{assessment.symbol} short target/invalidation geometry is invalid")
        distance_percent = abs(target / current - 1) * 100
        if distance_percent > self._config.max_target_distance_percent:
            raise ValueError(
                f"{assessment.symbol} target distance {distance_percent:.2f}% "
                "exceeds configured limit"
            )

    @staticmethod
    def _usage(stdout: str) -> dict[str, int]:
        usage: dict[str, int] = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            candidate = event.get("usage")
            if not isinstance(candidate, Mapping):
                continue
            parsed = {
                str(key): int(value)
                for key, value in candidate.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            if parsed:
                usage = parsed
        return usage

    @staticmethod
    def _feedback(error: str) -> str:
        return (
            "\n\n# 上一次输出未通过程序校验\n"
            + error
            + "\n请重新核对输入证据并完整输出一个符合 Schema 的 JSON。"
        )

    @staticmethod
    def _process_failure(result: CodexProcessResult) -> str:
        stderr = result.stderr.strip().replace("\r", " ").replace("\n", " ")
        stdout = result.stdout.strip().replace("\r", " ").replace("\n", " ")
        detail = f"stderr={stderr or 'none'}; stdout={stdout or 'none'}"
        if len(detail) > 1600:
            detail = detail[-1597:] + "..."
        return f"Codex exited {result.return_code}: {detail or 'no diagnostics'}"

    @staticmethod
    def _concise_error(error: Exception) -> str:
        detail = str(error).replace("\r", " ").replace("\n", " ")
        return detail[:1000]


def _codex_command_prefix(codex_executable: str) -> tuple[str, ...]:
    if os.name != "nt" or codex_executable.lower() not in {"codex", "codex.cmd"}:
        return (codex_executable,)
    command_wrapper = shutil.which("codex.cmd")
    node_executable = shutil.which("node.exe")
    if command_wrapper is not None and node_executable is not None:
        codex_script = (
            Path(command_wrapper).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        )
        if codex_script.is_file():
            return node_executable, str(codex_script.resolve())
    return (codex_executable,)


def _strict_output_schema(value: object) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        pattern = value.get("pattern")
        if isinstance(pattern, str) and "(?" in pattern:
            value.pop("pattern", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for child in value.values():
            _strict_output_schema(child)
    elif isinstance(value, list):
        for child in value:
            _strict_output_schema(child)


def _evidence_values(
    bundle: EvidenceBundle,
    suffix: str,
) -> Mapping[str, Any] | None:
    item = next(
        (item for item in bundle.evidence_items if item.evidence_id.endswith(suffix)),
        None,
    )
    return item.values if item is not None else None


def _number(values: Mapping[str, Any], key: str) -> float | None:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _atr_distance(values: Mapping[str, Any], *, high: bool) -> float | None:
    explicit_key = (
        "drawdown_from_rolling_high_atr"
        if high
        else "rebound_from_rolling_low_atr"
    )
    explicit = _number(values, explicit_key)
    if explicit is not None:
        return explicit
    close = _number(values, "latest_close")
    atr = _number(values, "atr_14")
    boundary = _number(values, "rolling_high_20" if high else "rolling_low_20")
    if close is None or atr is None or atr <= 0 or boundary is None:
        return None
    return max(0.0, (boundary - close) / atr if high else (close - boundary) / atr)


def _pivot_age_bars(
    values: Mapping[str, Any],
    *,
    high: bool,
    timeframe_minutes: int,
) -> int | None:
    prefix = "pivot_high" if high else "pivot_low"
    explicit = _number(values, f"{prefix}_age_bars")
    if explicit is not None and explicit >= 0:
        return round(explicit)
    confirmed = values.get(f"{prefix}_confirmed_at")
    latest = values.get("latest_completed_close_time")
    if not isinstance(confirmed, str) or not isinstance(latest, str):
        return None
    try:
        confirmed_at = datetime.fromisoformat(confirmed)
        latest_at = datetime.fromisoformat(latest)
    except ValueError:
        return None
    seconds = (latest_at - confirmed_at).total_seconds()
    if seconds < 0:
        return None
    return round(seconds / (timeframe_minutes * 60))
