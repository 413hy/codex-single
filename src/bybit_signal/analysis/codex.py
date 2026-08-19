from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from bybit_signal.analysis.tool_registry import ReadOnlyAnalysisToolRegistry
from bybit_signal.config import AnalysisConfig, MonitoringConfig
from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    Direction,
    MonitoringMetric,
    MonitoringReviewDecision,
)
from bybit_signal.domain.models import (
    AnalysisToolResult,
    CandidateAssessment,
    EvidenceBundle,
    ModelAnalysisResponse,
    ModelMonitoringReviewResponse,
    ModelTurnResponse,
    MonitoringDirective,
    MonitoringRuleReview,
    ToolAssessment,
)


class CodexAnalysisError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.code = code
        self.diagnostics = diagnostics
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
    evidence_bundles: tuple[EvidenceBundle, ...] = ()
    tool_results: tuple[AnalysisToolResult, ...] = ()
    attempt_diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CodexMonitoringReviewResult:
    response: ModelMonitoringReviewResponse
    context_sha256: str
    latency_ms: int
    usage: dict[str, int]
    attempts: int = 1
    attempt_diagnostics: tuple[dict[str, Any], ...] = ()
    repair_diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class MonitoringActivationResult:
    assessment: CandidateAssessment
    bundle_generated_at: datetime
    source_snapshot_sha256: str
    current_values: dict[str, str]
    dropped_directives: tuple[dict[str, str], ...]


class CodexProcessRunner(Protocol):
    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        stdin: str,
    ) -> CodexProcessResult: ...


SchemaModel = TypeVar("SchemaModel", bound=BaseModel)
_STRUCTURAL_MONITORING_METRICS = frozenset(
    {
        MonitoringMetric.COMPLETED_1M_CLOSE,
        MonitoringMetric.COMPLETED_5M_CLOSE,
        MonitoringMetric.COMPLETED_15M_CLOSE,
        MonitoringMetric.COMPLETED_30M_CLOSE,
        MonitoringMetric.COMPLETED_1H_CLOSE,
    }
)
_SUPPORTED_MONITORING_METRICS = frozenset(
    {
        MonitoringMetric.LAST_PRICE,
        MonitoringMetric.MARK_PRICE,
        *_STRUCTURAL_MONITORING_METRICS,
        MonitoringMetric.TURNOVER_1M,
        MonitoringMetric.TRADE_DELTA_30S,
        MonitoringMetric.SPREAD_BPS,
        MonitoringMetric.ORDERBOOK_IMBALANCE_L5,
        MonitoringMetric.OPEN_INTEREST,
        MonitoringMetric.FUNDING_RATE,
        MonitoringMetric.LIQUIDATION_NOTIONAL_1M,
    }
)


async def run_codex_process(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    stdin: str,
) -> CodexProcessResult:
    process_options = _subprocess_platform_options(os.name)
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
                **process_options,
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
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        raise
    except TimeoutError as error:
        await _terminate_process_tree(process)
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


def _subprocess_platform_options(platform_name: str) -> dict[str, Any]:
    if platform_name == "nt":
        return {
            "creationflags": 0x08000000
            | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        }
    # Give every Codex invocation its own POSIX process group. On a timeout the
    # service must terminate the whole tree, not leave helper processes behind.
    return {"start_new_session": True}


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    platform_name: str | None = None,
    kill_process_group: Callable[[int, int], None] | None = None,
) -> None:
    if process.returncode is not None:
        return
    platform_name = platform_name or os.name
    if platform_name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        await killer.communicate()
    else:
        if kill_process_group is None:
            candidate = getattr(os, "killpg", None)
            if not callable(candidate):
                process.kill()
                candidate = None
            kill_process_group = candidate
        with contextlib.suppress(ProcessLookupError):
            if kill_process_group is not None:
                kill_process_group(process.pid, 9)
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


class CodexAnalyzer:
    def __init__(
        self,
        config: AnalysisConfig,
        *,
        workspace: Path,
        runtime_root: Path,
        prompt_path: Path,
        monitoring_config: MonitoringConfig | None = None,
        process_runner: CodexProcessRunner = run_codex_process,
        codex_executable: str = "codex",
        clock: Callable[[], datetime] | None = None,
        tool_registry: ReadOnlyAnalysisToolRegistry | None = None,
        monitoring_review_prompt_path: Path | None = None,
        model_skill_path: Path | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace.resolve()
        self._runtime_root = runtime_root.resolve()
        self._prompt_path = prompt_path.resolve()
        self._monitoring_config = monitoring_config or MonitoringConfig()
        self._process_runner = process_runner
        self._codex_command_prefix = _codex_command_prefix(codex_executable)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tool_registry = tool_registry
        self._monitoring_review_prompt_path = (
            monitoring_review_prompt_path.resolve()
            if monitoring_review_prompt_path is not None
            else self._prompt_path.with_name("monitoring_review_zh.md")
        )
        self._model_skill_path = (
            model_skill_path.resolve()
            if model_skill_path is not None
            else Path(__file__).resolve().parents[3]
            / ".agents"
            / "skills"
            / "analyze-bybit-ultrashort-signals"
        )

    async def analyze(
        self,
        *,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        tracked_symbols: Sequence[str] = (),
        trigger_reasons: Sequence[str] = (),
        mode: CycleMode = CycleMode.SCHEDULED,
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
        if self._tool_registry is not None and self._config.tool_protocol_enabled:
            return await self._analyze_with_tools(
                analysis_id=analysis_id,
                bundles=tuple(bundles),
                tracked_symbols=tracked_symbols,
                trigger_reasons=trigger_reasons,
                mode=mode,
            )
        requested_at = self._clock()
        outlook_windows = _candle_outlook_windows(requested_at)
        context = self._context(
            analysis_id=analysis_id,
            bundles=bundles,
            tracked_symbols=tracked_symbols,
            trigger_reasons=trigger_reasons,
            mode=mode,
            primary_signal_count=self._config.primary_signal_count,
            requested_at=requested_at,
            monitoring_config=self._monitoring_config,
        )
        context_json = json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        base_prompt = (
            self._model_handbook()
            + "\n\n"
            + self._prompt_path.read_text(encoding="utf-8")
        )
        validation_feedback = ""
        total_latency = 0
        last_error = "no model attempt completed"
        usage: dict[str, int] = {}
        self._runtime_root.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, self._config.max_attempts + 1):
            prompt = base_prompt + "\n\n# 本轮输入\n" + context_json + validation_feedback
            with tempfile.TemporaryDirectory(prefix="bybit-signal-codex-analysis-") as temporary:
                temporary_root = Path(temporary)
                schema_path = temporary_root / "response.schema.json"
                output_path = temporary_root / "last-message.json"
                schema = ModelAnalysisResponse.model_json_schema()
                _strict_output_schema(schema)
                schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False),
                    encoding="utf-8",
                )
                command = self._command(schema_path, output_path, temporary_root)
                process = await self._process_runner(
                    command,
                    temporary_root,
                    self._config.timeout_seconds,
                    prompt,
                )
                total_latency += process.latency_ms
                usage = self._usage(process.stdout) or usage
                if process.return_code != 0:
                    last_error = self._process_failure(process)
                    permanent_code = self._permanent_failure_code(process)
                    if permanent_code is not None:
                        raise CodexAnalysisError(permanent_code, last_error)
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
                    response = _annotate_monitoring_metadata(
                        response, bundles
                    )
                    self._validate_response(
                        response=response,
                        analysis_id=analysis_id,
                        bundles=bundles,
                        mode=mode,
                        outlook_windows=outlook_windows,
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
                    evidence_bundles=tuple(bundles),
                    attempt_diagnostics=(
                        {
                            "call": attempt,
                            "status": "FINAL_ACCEPTED",
                            "latency_ms": process.latency_ms,
                        },
                    ),
                )
        raise CodexAnalysisError(
            "INVALID_MODEL_OUTPUT",
            f"Codex failed after {self._config.max_attempts} attempt(s): {last_error}",
        )

    async def _analyze_with_tools(
        self,
        *,
        analysis_id: str,
        bundles: tuple[EvidenceBundle, ...],
        tracked_symbols: Sequence[str],
        trigger_reasons: Sequence[str],
        mode: CycleMode,
    ) -> CodexAnalysisResult:
        if self._tool_registry is None:
            raise CodexAnalysisError("TOOL_REGISTRY_MISSING", "tool registry is unavailable")
        requested_at = self._clock()
        outlook_windows = _candle_outlook_windows(requested_at)
        base_prompt = (
            self._model_handbook()
            + "\n\n"
            + self._prompt_path.read_text(encoding="utf-8")
        )
        current_bundles = bundles
        tool_results: list[AnalysisToolResult] = []
        seen_requests: set[tuple[str, str | None, str]] = set()
        validation_feedback = ""
        total_latency = 0
        usage: dict[str, int] = {}
        model_calls = 0
        tool_rounds = 0
        failed_model_calls = 0
        last_error = "tool protocol did not produce a final response"
        attempt_diagnostics: list[dict[str, Any]] = []
        maximum_model_calls = self._config.max_tool_rounds + self._config.max_attempts + 1
        self._runtime_root.mkdir(parents=True, exist_ok=True)

        while model_calls < maximum_model_calls:
            context = self._context(
                analysis_id=analysis_id,
                bundles=current_bundles,
                tracked_symbols=tracked_symbols,
                trigger_reasons=trigger_reasons,
                mode=mode,
                primary_signal_count=self._config.primary_signal_count,
                requested_at=requested_at,
                monitoring_config=self._monitoring_config,
            )
            context["tool_protocol"] = {
                "enabled": True,
                "remaining_rounds": max(0, self._config.max_tool_rounds - tool_rounds),
                "remaining_calls": max(0, self._config.max_tool_calls - len(tool_results)),
                "allowed_tools": [
                    "latest_market",
                    "short_candles",
                    "market_context",
                    "depth_and_trades",
                    "derivatives",
                    "cross_exchange",
                    "extended_candles",
                ],
                "contract": (
                    "Return action=FINAL with final response when evidence is sufficient. "
                    "Otherwise return action=TOOL_REQUESTS with only bounded read-only "
                    "requests. Never request shell, files, arbitrary URLs, accounts or orders."
                ),
            }
            context["completed_tool_results"] = [
                result.model_dump(mode="json") for result in tool_results
            ]
            context_json = json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            prompt = (
                base_prompt
                + "\n\n# 宿主管理工具轮次\n"
                + "本轮只能按 tool_protocol 返回 TOOL_REQUESTS 或 FINAL。"
                + "工具由宿主执行, 模型自身不得访问外部环境。\n\n# 本轮输入\n"
                + context_json
                + validation_feedback
            )
            try:
                turn, process = await self._invoke_structured(ModelTurnResponse, prompt)
            except CodexAnalysisError as error:
                if error.code in {
                    "CODEX_CREDITS_EXHAUSTED",
                    "CODEX_AUTH_REQUIRED",
                    "CODEX_MODEL_UNAVAILABLE",
                }:
                    raise
                last_error = self._concise_error(error)
                attempt_diagnostics.append(
                    {
                        "call": model_calls + 1,
                        "status": "PROCESS_ERROR",
                        "code": error.code,
                        "message": last_error,
                    }
                )
                validation_feedback = self._feedback(last_error)
                model_calls += 1
                failed_model_calls += 1
                if failed_model_calls >= self._config.max_attempts:
                    break
                continue
            except (ValidationError, ValueError) as error:
                last_error = self._concise_error(error)
                attempt_diagnostics.append(
                    {
                        "call": model_calls + 1,
                        "status": "SCHEMA_REJECTED",
                        "message": last_error,
                    }
                )
                validation_feedback = self._feedback(last_error)
                model_calls += 1
                failed_model_calls += 1
                if failed_model_calls >= self._config.max_attempts:
                    break
                continue
            model_calls += 1
            total_latency += process.latency_ms
            usage = self._usage(process.stdout) or usage
            if turn.action.value == "FINAL":
                if turn.final is None:
                    last_error = "FINAL turn omitted final response"
                    attempt_diagnostics.append(
                        {
                            "call": model_calls,
                            "status": "FINAL_REJECTED",
                            "latency_ms": process.latency_ms,
                            "message": last_error,
                        }
                    )
                    validation_feedback = self._feedback(last_error)
                    failed_model_calls += 1
                    if failed_model_calls >= self._config.max_attempts:
                        break
                    continue
                try:
                    final = _annotate_monitoring_metadata(
                        turn.final, current_bundles
                    )
                    self._validate_response(
                        response=final,
                        analysis_id=analysis_id,
                        bundles=current_bundles,
                        mode=mode,
                        outlook_windows=outlook_windows,
                    )
                except (ValidationError, ValueError) as error:
                    last_error = self._concise_error(error)
                    attempt_diagnostics.append(
                        {
                            "call": model_calls,
                            "status": "FINAL_REJECTED",
                            "latency_ms": process.latency_ms,
                            "message": last_error,
                        }
                    )
                    validation_feedback = self._feedback(last_error)
                    failed_model_calls += 1
                    if failed_model_calls >= self._config.max_attempts:
                        break
                    continue
                final_context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
                accepted_diagnostic: dict[str, Any] = {
                    "call": model_calls,
                    "status": "FINAL_ACCEPTED",
                    "latency_ms": process.latency_ms,
                }
                attempt_diagnostics.append(accepted_diagnostic)
                return CodexAnalysisResult(
                    response=final,
                    context_sha256=final_context_sha256,
                    latency_ms=total_latency,
                    attempts=model_calls,
                    usage=usage,
                    evidence_bundles=current_bundles,
                    tool_results=tuple(tool_results),
                    attempt_diagnostics=tuple(attempt_diagnostics),
                )
            if tool_rounds >= self._config.max_tool_rounds:
                last_error = "tool round limit reached; return FINAL using available evidence"
                attempt_diagnostics.append(
                    {
                        "call": model_calls,
                        "status": "TOOL_REQUEST_REJECTED",
                        "latency_ms": process.latency_ms,
                        "message": last_error,
                    }
                )
                validation_feedback = self._feedback(last_error)
                failed_model_calls += 1
                if failed_model_calls >= self._config.max_attempts:
                    break
                continue
            remaining = self._config.max_tool_calls - len(tool_results)
            if remaining <= 0 or len(turn.requests) > remaining:
                last_error = "tool call limit exceeded"
                attempt_diagnostics.append(
                    {
                        "call": model_calls,
                        "status": "TOOL_REQUEST_REJECTED",
                        "latency_ms": process.latency_ms,
                        "message": last_error,
                    }
                )
                validation_feedback = self._feedback(last_error)
                failed_model_calls += 1
                if failed_model_calls >= self._config.max_attempts:
                    break
                continue
            unique_requests = []
            duplicate = False
            for request in turn.requests:
                identity = (
                    request.tool,
                    request.symbol,
                    request.arguments.model_dump_json(),
                )
                if identity in seen_requests:
                    duplicate = True
                    break
                seen_requests.add(identity)
                unique_requests.append(request)
            if duplicate:
                last_error = "duplicate tool request; use prior result and return FINAL"
                attempt_diagnostics.append(
                    {
                        "call": model_calls,
                        "status": "TOOL_REQUEST_REJECTED",
                        "latency_ms": process.latency_ms,
                        "message": last_error,
                    }
                )
                validation_feedback = self._feedback(last_error)
                failed_model_calls += 1
                if failed_model_calls >= self._config.max_attempts:
                    break
                continue
            results = await self._tool_registry.execute_many(
                unique_requests,
                allowed_symbols=frozenset(bundle.symbol for bundle in current_bundles),
            )
            tool_results.extend(results)
            attempt_diagnostics.append(
                {
                    "call": model_calls,
                    "status": "TOOL_REQUESTS_EXECUTED",
                    "latency_ms": process.latency_ms,
                    "requests": [request.request_id for request in unique_requests],
                }
            )
            current_bundles = _augment_bundles(current_bundles, results)
            tool_rounds += 1
            validation_feedback = ""
        raise CodexAnalysisError(
            "INVALID_MODEL_OUTPUT",
            f"Codex tool protocol failed after {model_calls} model call(s): {last_error}",
            diagnostics=tuple(attempt_diagnostics),
        )

    async def review_monitoring(
        self,
        *,
        analysis_id: str,
        assessments: Sequence[CandidateAssessment],
        bundles: Sequence[EvidenceBundle],
        repair_reasons: Mapping[str, Sequence[str]] | None = None,
        maximum_repair_attempts: int | None = None,
    ) -> CodexMonitoringReviewResult:
        if not assessments or not bundles:
            raise CodexAnalysisError(
                "EMPTY_MONITORING_REVIEW",
                "monitoring review requires selected assessments and fresh evidence",
            )
        if not self._monitoring_review_prompt_path.is_file():
            raise CodexAnalysisError(
                "MONITORING_PROMPT_MISSING",
                f"monitoring review prompt is missing: {self._monitoring_review_prompt_path}",
            )
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        expected = {assessment.symbol for assessment in assessments}
        if expected != set(bundle_by_symbol):
            raise CodexAnalysisError(
                "MONITORING_CONTEXT_MISMATCH",
                "monitoring review assessments and evidence symbols differ",
            )
        assessment_by_symbol = {
            assessment.symbol: assessment for assessment in assessments
        }
        ordered_symbols = tuple(assessment.symbol for assessment in assessments)
        handbook = self._model_handbook()
        review_prompt = self._monitoring_review_prompt_path.read_text(encoding="utf-8")
        requested_repair_budget = (
            0 if maximum_repair_attempts is None else maximum_repair_attempts
        )
        if not 0 <= requested_repair_budget <= self._config.monitoring_review_repair_attempts:
            raise CodexAnalysisError(
                "MONITORING_REPAIR_BUDGET_INVALID",
                "monitoring repair budget must be within the configured maximum",
            )
        # A valid REJECTED review means that no useful, non-noise threshold exists.
        # It is a normal no-monitoring outcome and must not be turned into repeated
        # semantic model calls. Delivery-time activation failures are re-collected
        # and retried by SignalCycleService, while malformed output is retried by
        # the technical-attempt loop below.
        maximum_repairs = 0
        supplied_reasons = repair_reasons or {}
        unknown_reason_symbols = set(supplied_reasons) - expected
        if unknown_reason_symbols:
            raise CodexAnalysisError(
                "MONITORING_REPAIR_CONTEXT_MISMATCH",
                "monitoring repair reasons contain unknown symbols: "
                + ", ".join(sorted(unknown_reason_symbols)),
            )
        pending_symbols = ordered_symbols
        final_reviews: dict[str, MonitoringRuleReview] = {}
        repair_state: dict[str, dict[str, Any]] = {
            symbol: {
                "symbol": symbol,
                "initial_decision": None,
                "repair_attempts": 0,
                "repair_history": [],
                "final_decision": None,
                "exhausted": False,
            }
            for symbol in ordered_symbols
        }
        rejection_history: dict[str, list[str]] = {
            symbol: [str(reason) for reason in supplied_reasons.get(symbol, ())]
            for symbol in ordered_symbols
        }
        context_documents: list[str] = []
        total_latency = 0
        total_usage: dict[str, int] = {}
        total_calls = 0
        diagnostics: list[dict[str, Any]] = []
        for repair_attempt in range(0, maximum_repairs + 1):
            current_assessments = tuple(
                assessment_by_symbol[symbol] for symbol in pending_symbols
            )
            current_bundles = tuple(bundle_by_symbol[symbol] for symbol in pending_symbols)
            context: dict[str, Any] = {
                "schema_version": 1,
                "analysis_id": analysis_id,
                "review_mode": "MONITORING_REVIEW",
                "reviewed_at": self._clock().isoformat(),
                "policy": {
                    "rules_per_symbol": {"minimum": 0, "maximum": 3},
                    "purpose": "counter_direction_structure_threat_reanalysis_wake",
                    "microstructure_requires_price_conjunction": True,
                    "take_profit_is_display_only": True,
                    "automatic_repair_attempts": maximum_repairs,
                },
                # The approximate take-profit is deliberately absent here. It is useful
                # in the delivered signal but has no role in direction-threat rules.
                "signals": [
                    assessment.model_dump(mode="json", exclude={"take_profit"})
                    for assessment in current_assessments
                ],
                "fresh_evidence_bundles": [
                    _model_bundle_payload(bundle, self._monitoring_config)
                    for bundle in current_bundles
                ],
            }
            if repair_attempt or any(rejection_history[symbol] for symbol in pending_symbols):
                context["repair_context"] = {
                    "is_automatic_repair": True,
                    "repair_attempt": max(1, repair_attempt),
                    "maximum_repair_attempts": maximum_repairs,
                    "remaining_after_this": maximum_repairs - repair_attempt,
                    "instruction": (
                        "Only repair the listed rejected symbols. Address each prior "
                        "model rejection or activation error using the fresh baselines "
                        "in this request and a different evidence-grounded structure; "
                        "do not merely move a threshold. REJECTED remains valid when no "
                        "rule is safe and useful."
                    ),
                    "symbols": [
                        {
                            "symbol": symbol,
                            "previous_rejections": rejection_history[symbol],
                        }
                        for symbol in pending_symbols
                    ],
                }
            context_json = json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            context_documents.append(context_json)
            base_prompt = (
                handbook
                + "\n\n"
                + review_prompt
                + "\n\n# 本轮输入\n"
                + context_json
            )
            feedback = ""
            batch_response: ModelMonitoringReviewResponse | None = None
            batch_latency = 0
            last_code = "MONITORING_REVIEW_INVALID"
            last_error = "monitoring review did not produce a valid response"
            for technical_attempt in range(1, self._config.max_attempts + 1):
                attempt_code = "MONITORING_REVIEW_NOT_EXECUTABLE"
                total_calls += 1
                try:
                    response, process = await self._invoke_structured(
                        ModelMonitoringReviewResponse,
                        base_prompt + feedback,
                    )
                    total_latency += process.latency_ms
                    batch_latency = process.latency_ms
                    for key, value in self._usage(process.stdout).items():
                        total_usage[key] = total_usage.get(key, 0) + value
                    if response.analysis_id != analysis_id:
                        attempt_code = "MONITORING_REVIEW_ID_MISMATCH"
                        raise ValueError(
                            "monitoring review analysis_id differs from the request"
                        )
                    actual = {review.symbol for review in response.reviews}
                    expected_batch = set(pending_symbols)
                    if actual != expected_batch:
                        attempt_code = "MONITORING_REVIEW_SYMBOL_MISMATCH"
                        raise ValueError(
                            f"expected {sorted(expected_batch)}, got {sorted(actual)}"
                        )
                    response = response.model_copy(
                        update={
                            "reviews": tuple(
                                _normalize_reviewed_baselines(
                                    review, bundle_by_symbol[review.symbol]
                                )
                                for review in response.reviews
                            )
                        }
                    )
                    for review in response.reviews:
                        _validate_reviewed_directives(
                            review.directives,
                            bundle_by_symbol[review.symbol],
                            assessment_by_symbol[review.symbol],
                        )
                except CodexAnalysisError as error:
                    last_code = error.code
                    last_error = self._concise_error(error)
                    diagnostics.append(
                        {
                            "call": total_calls,
                            "repair_attempt": repair_attempt,
                            "technical_attempt": technical_attempt,
                            "symbols": pending_symbols,
                            "status": "PROCESS_ERROR",
                            "code": error.code,
                            "message": last_error,
                        }
                    )
                    if error.code in {
                        "CODEX_CREDITS_EXHAUSTED",
                        "CODEX_AUTH_REQUIRED",
                        "CODEX_MODEL_UNAVAILABLE",
                    }:
                        raise CodexAnalysisError(
                            error.code,
                            last_error,
                            diagnostics=tuple(diagnostics),
                        ) from error
                except (ValidationError, ValueError) as error:
                    last_code = attempt_code
                    last_error = self._concise_error(error)
                    diagnostics.append(
                        {
                            "call": total_calls,
                            "repair_attempt": repair_attempt,
                            "technical_attempt": technical_attempt,
                            "symbols": pending_symbols,
                            "status": "REVIEW_REJECTED",
                            "code": last_code,
                            "message": last_error,
                        }
                    )
                else:
                    batch_response = response
                    diagnostics.append(
                        {
                            "call": total_calls,
                            "repair_attempt": repair_attempt,
                            "technical_attempt": technical_attempt,
                            "symbols": pending_symbols,
                            "status": "REVIEW_ACCEPTED",
                            "latency_ms": process.latency_ms,
                            "decisions": {
                                review.symbol: review.decision.value
                                for review in response.reviews
                            },
                        }
                    )
                    break
                feedback = self._feedback(last_error)
            if batch_response is None:
                raise CodexAnalysisError(
                    last_code,
                    (
                        "Codex monitoring review failed after "
                        f"{self._config.max_attempts} technical attempt(s): {last_error}"
                    ),
                    diagnostics=tuple(diagnostics),
                )

            next_pending: list[str] = []
            review_by_symbol = {
                review.symbol: review for review in batch_response.reviews
            }
            for symbol in pending_symbols:
                review = review_by_symbol[symbol]
                state = repair_state[symbol]
                history = state["repair_history"]
                if not isinstance(history, list):
                    raise RuntimeError("monitoring repair history is not mutable")
                if state["initial_decision"] is None:
                    state["initial_decision"] = review.decision.value
                if repair_attempt:
                    state["repair_attempts"] = repair_attempt
                history.append(
                    {
                        "repair_attempt": repair_attempt,
                        "decision": review.decision.value,
                        "rationale": review.rationale,
                        "latency_ms": batch_latency,
                    }
                )
                if (
                    review.decision is MonitoringReviewDecision.REJECTED
                    and repair_attempt < maximum_repairs
                ):
                    rejection_history[symbol].append(review.rationale)
                    next_pending.append(symbol)
                    continue
                final_reviews[symbol] = review
                state["final_decision"] = review.decision.value
                state["exhausted"] = False
            if not next_pending:
                break
            pending_symbols = tuple(
                symbol for symbol in ordered_symbols if symbol in next_pending
            )

        if set(final_reviews) != expected:
            missing = sorted(expected - set(final_reviews))
            raise CodexAnalysisError(
                "MONITORING_REPAIR_INCOMPLETE",
                f"monitoring repair did not finalize symbols: {missing}",
                diagnostics=tuple(diagnostics),
            )
        combined_context = "\n".join(context_documents)
        return CodexMonitoringReviewResult(
            response=ModelMonitoringReviewResponse(
                analysis_id=analysis_id,
                reviews=tuple(final_reviews[symbol] for symbol in ordered_symbols),
            ),
            context_sha256=hashlib.sha256(
                combined_context.encode("utf-8")
            ).hexdigest(),
            latency_ms=total_latency,
            usage=total_usage,
            attempts=total_calls,
            attempt_diagnostics=tuple(diagnostics),
            repair_diagnostics=tuple(repair_state[symbol] for symbol in ordered_symbols),
        )

    def _model_handbook(self) -> str:
        skill_path = self._model_skill_path / "SKILL.md"
        contract_path = self._model_skill_path / "references" / "operating-contract.md"
        if not skill_path.is_file() or not contract_path.is_file():
            raise CodexAnalysisError(
                "MODEL_HANDBOOK_MISSING",
                f"model operating handbook is incomplete under {self._model_skill_path}",
            )
        return (
            "# 模型操作手册 (运行时显式注入)\n"
            + skill_path.read_text(encoding="utf-8")
            + "\n\n"
            + contract_path.read_text(encoding="utf-8")
        )

    async def _invoke_structured(
        self,
        model: type[SchemaModel],
        prompt: str,
    ) -> tuple[SchemaModel, CodexProcessResult]:
        with tempfile.TemporaryDirectory(prefix="bybit-signal-codex-tool-turn-") as temporary:
            temporary_root = Path(temporary)
            schema_path = temporary_root / "response.schema.json"
            output_path = temporary_root / "last-message.json"
            schema = model.model_json_schema()
            _strict_output_schema(schema)
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            process = await self._process_runner(
                self._command(schema_path, output_path, temporary_root),
                temporary_root,
                self._config.timeout_seconds,
                prompt,
            )
            if process.return_code != 0:
                message = self._process_failure(process)
                permanent_code = self._permanent_failure_code(process)
                raise CodexAnalysisError(permanent_code or "CODEX_PROCESS_FAILED", message)
            if not output_path.is_file():
                raise CodexAnalysisError(
                    "CODEX_OUTPUT_MISSING", "Codex did not create its last-message output"
                )
            if output_path.stat().st_size > 2 * 1024 * 1024:
                raise CodexAnalysisError(
                    "CODEX_OUTPUT_TOO_LARGE", "Codex last-message output exceeds 2 MiB"
                )
            value = model.model_validate_json(output_path.read_text(encoding="utf-8"))
            return value, process

    def _command(
        self,
        schema_path: Path,
        output_path: Path,
        working_directory: Path,
    ) -> tuple[str, ...]:
        return (
            *self._codex_command_prefix,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--disable",
            "skill_search",
            "--disable",
            "shell_tool",
            "--disable",
            "plugins",
            "--disable",
            "apps",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--model",
            self._config.model,
            "-c",
            f'model_reasoning_effort="{self._config.reasoning_effort}"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--cd",
            str(working_directory),
            "-",
        )

    @staticmethod
    def _context(
        *,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        tracked_symbols: Sequence[str],
        trigger_reasons: Sequence[str],
        mode: CycleMode,
        primary_signal_count: int,
        requested_at: datetime,
        monitoring_config: MonitoringConfig,
    ) -> dict[str, Any]:
        outlook_windows = _candle_outlook_windows(requested_at)
        cutoffs = [bundle.generated_at for bundle in bundles]
        market_context = next(
            (bundle.market_context for bundle in bundles if bundle.market_context is not None),
            None,
        )
        return {
            "schema_version": 1,
            "analysis_id": analysis_id,
            "analysis_mode": mode.value,
            "requested_at": requested_at.isoformat(),
            "batch_evidence_cutoff_utc": max(cutoffs).isoformat(),
            "bundle_cutoff_skew_seconds": (max(cutoffs) - min(cutoffs)).total_seconds(),
            "candle_outlook_windows_utc": {
                name: {"start": start.isoformat(), "end": end.isoformat()}
                for name, (start, end) in outlook_windows.items()
            },
            "analysis_policy": {
                "required_primary_signals": (
                    primary_signal_count if mode is CycleMode.SCHEDULED else 0
                ),
                "monitoring_valid_for_seconds": {"minimum": 1800, "maximum": 3600},
                "monitoring_directives_per_primary": {"minimum": 0, "maximum": 3},
            },
            "tracked_symbols_without_prior_direction": sorted(set(tracked_symbols)),
            "emergency_trigger_reasons": (
                [
                    str(reason).replace("\r", " ").replace("\n", " ")[:1000]
                    for reason in trigger_reasons[:8]
                ]
                if mode is CycleMode.EMERGENCY
                else []
            ),
            "batch_market_context": (
                market_context.model_dump(mode="json") if market_context is not None else None
            ),
            "evidence_bundles": [
                _model_bundle_payload(bundle, monitoring_config) for bundle in bundles
            ],
        }

    def _validate_response(
        self,
        *,
        response: ModelAnalysisResponse,
        analysis_id: str,
        bundles: Sequence[EvidenceBundle],
        mode: CycleMode,
        outlook_windows: Mapping[str, tuple[datetime, datetime]],
    ) -> None:
        if response.analysis_id != analysis_id:
            raise ValueError("analysis_id does not match the request")
        if response.mode is not mode:
            raise ValueError("analysis mode does not match the request")
        expected = {bundle.symbol for bundle in bundles}
        actual = {assessment.symbol for assessment in response.assessments}
        if actual != expected:
            raise ValueError(
                f"assessment symbols differ: expected {sorted(expected)}, got {sorted(actual)}"
            )
        selected = sorted(
            (
                assessment
                for assessment in response.assessments
                if assessment.selection_rank is not None
            ),
            key=lambda assessment: assessment.selection_rank or 0,
        )
        if mode is CycleMode.SCHEDULED:
            if [assessment.selection_rank for assessment in selected] != [1, 2]:
                raise ValueError("scheduled analysis must select exactly ranks 1 and 2")
        elif selected:
            raise ValueError("emergency analysis cannot select scheduled primary signals")
        elif len(response.assessments) != 1:
            raise ValueError("emergency analysis requires exactly one assessment")
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        for assessment in response.assessments:
            bundle = bundle_by_symbol[assessment.symbol]
            known = {item.evidence_id for item in bundle.evidence_items}
            referenced = set(assessment.evidence_ids)
            outlooks = {
                "forming_15m": assessment.forming_15m,
                "forming_30m": assessment.forming_30m,
                "forming_1h": assessment.forming_1h,
                "next_15m": assessment.next_15m,
            }
            visible = assessment.selection_rank is not None or mode is CycleMode.EMERGENCY
            if visible and (
                assessment.direction is None
                or assessment.invalidation is None
                or not assessment.evidence_ids
                or any(outlook is None for outlook in outlooks.values())
            ):
                raise ValueError(
                    f"{assessment.symbol} visible signal requires direction, invalidation, "
                    "evidence and all four candle outlooks"
                )
            if not visible and any(outlook is not None for outlook in outlooks.values()):
                raise ValueError(
                    f"{assessment.symbol} non-selected assessment cannot contain outlooks"
                )
            for name, outlook in outlooks.items():
                expected_start, expected_end = outlook_windows[name]
                if outlook is not None and (
                    outlook.window_start != expected_start or outlook.window_end != expected_end
                ):
                    raise ValueError(
                        f"{assessment.symbol} {name} window differs from the requested window"
                    )
            if assessment.invalidation is not None:
                referenced.update(assessment.invalidation.evidence_ids)
            family_ids: set[str] = set()
            for directive in assessment.monitoring_directives:
                referenced.update(directive.evidence_ids)
                for confirmation in directive.confirmations:
                    referenced.update(confirmation.evidence_ids)
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
                if directive.metric not in _SUPPORTED_MONITORING_METRICS:
                    raise ValueError(
                        f"{assessment.symbol} requested unsupported realtime metric "
                        f"{directive.metric.value}"
                    )
            if mode is CycleMode.SCHEDULED:
                if assessment.selection_rank is None and assessment.monitoring_directives:
                    raise ValueError(f"{assessment.symbol} non-selected assessment cannot monitor")
                if assessment.selection_rank is not None and len(
                    assessment.monitoring_directives
                ) > 3:
                    raise ValueError(
                        f"{assessment.symbol} selected signal allows at most 3 candidate directives"
                    )
            elif len(assessment.monitoring_directives) > 3:
                raise ValueError(
                    f"{assessment.symbol} emergency review allows at most 3 candidate directives"
                )
            if not referenced <= known:
                unknown = sorted(referenced - known)
                raise ValueError(f"{assessment.symbol} references unknown evidence: {unknown}")

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
    def _permanent_failure_code(result: CodexProcessResult) -> str | None:
        message = f"{result.stderr}\n{result.stdout}".lower()
        if "out of credits" in message or "credits exhausted" in message:
            return "CODEX_CREDITS_EXHAUSTED"
        if (
            "401 unauthorized" in message
            or "403 forbidden" in message
            or "not logged in" in message
            or "missing bearer or basic authentication" in message
        ):
            return "CODEX_AUTH_REQUIRED"
        if "model" in message and (
            "not found" in message or "does not exist" in message or "not available" in message
        ):
            return "CODEX_MODEL_UNAVAILABLE"
        return None

    @staticmethod
    def _concise_error(error: Exception) -> str:
        detail = str(error).replace("\r", " ").replace("\n", " ")
        return detail[:1000]


def _candle_outlook_windows(
    requested_at: datetime,
) -> dict[str, tuple[datetime, datetime]]:
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("requested_at must be timezone-aware")
    base = requested_at.replace(second=0, microsecond=0)
    forming_15m_start = base.replace(minute=base.minute - base.minute % 15)
    forming_30m_start = base.replace(minute=base.minute - base.minute % 30)
    forming_1h_start = base.replace(minute=0)
    forming_15m_end = forming_15m_start + timedelta(minutes=15)
    return {
        "forming_15m": (forming_15m_start, forming_15m_end),
        "forming_30m": (forming_30m_start, forming_30m_start + timedelta(minutes=30)),
        "forming_1h": (forming_1h_start, forming_1h_start + timedelta(hours=1)),
        "next_15m": (forming_15m_end, forming_15m_end + timedelta(minutes=15)),
    }


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
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        result = float(value)
    except (InvalidOperation, ValueError):
        return None
    return result if math.isfinite(result) else None


def _model_bundle_payload(
    bundle: EvidenceBundle,
    monitoring_config: MonitoringConfig,
) -> dict[str, Any]:
    """Deduplicate batch-global context without changing frozen audit bundles."""

    payload = bundle.model_dump(mode="json", exclude={"market_context"})
    evidence = payload.get("evidence_items")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict) and item.get("category") == "market_context":
                item["values"] = {"batch_market_context_ref": "batch_market_context"}
                item["summary"] = (
                    "Use the single batch_market_context object; this evidence id remains "
                    "available for symbol-level citations"
                )
    payload["monitoring_observable_baselines"] = {
        metric.value: str(value)
        for metric, value in sorted(
            _monitoring_current_values(bundle).items(), key=lambda item: item[0].value
        )
    }
    return payload


def _annotate_monitoring_metadata(
    response: ModelAnalysisResponse,
    bundles: Sequence[EvidenceBundle],
) -> ModelAnalysisResponse:
    """Attach current observable baselines without accepting or rejecting strategy values."""

    bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
    assessments: list[CandidateAssessment] = []
    for assessment in response.assessments:
        bundle = bundle_by_symbol.get(assessment.symbol)
        if bundle is None:
            assessments.append(assessment)
            continue
        current_values = _monitoring_current_values(bundle)
        atr1 = _number(_evidence_values(bundle, ".PA.1M") or {}, "atr_14")
        directives = []
        for directive in assessment.monitoring_directives:
            current = current_values.get(directive.metric)
            distance_percent = None
            distance_atr = None
            if current is not None and directive.metric in {
                MonitoringMetric.LAST_PRICE,
                MonitoringMetric.MARK_PRICE,
                *_STRUCTURAL_MONITORING_METRICS,
            }:
                distance = abs(directive.threshold - current)
                distance_percent = distance / current * 100 if current else None
                if atr1 is not None and atr1 > 0:
                    distance_atr = distance / Decimal(str(atr1))
            confirmations = tuple(
                confirmation.model_copy(
                    update={
                        "current_value": current_values.get(
                            confirmation.metric, confirmation.current_value
                        )
                    }
                )
                for confirmation in directive.confirmations
            )
            directives.append(
                directive.model_copy(
                    update={
                        "current_value": current,
                        "distance_percent": distance_percent,
                        "distance_atr": distance_atr,
                        "confirmations": confirmations,
                    }
                )
            )
        assessments.append(
            assessment.model_copy(update={"monitoring_directives": tuple(directives)})
        )
    return response.model_copy(update={"assessments": tuple(assessments)})


def revalidate_monitoring_assessment(
    assessment: CandidateAssessment,
    bundle: EvidenceBundle,
    config: MonitoringConfig,
) -> MonitoringActivationResult:
    del config
    current_values = _monitoring_current_values(bundle)
    atr1 = _number(_evidence_values(bundle, ".PA.1M") or {}, "atr_14")
    directives: list[MonitoringDirective] = []
    for directive in assessment.monitoring_directives:
        if directive.metric not in _STRUCTURAL_MONITORING_METRICS:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} must use a completed-candle "
                "price structure as its primary condition"
            )
        current = current_values.get(directive.metric)
        if current is None:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} primary metric has no "
                "delivery-time baseline"
            )
        if directive.current_value is None:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} reviewed completed-candle "
                "baseline is missing before activation; rerun model monitoring review"
            )
        # The model owns the structural threshold; the host owns the observable
        # delivery baseline. A newer completed candle may safely rebase in either
        # direction while the reviewed condition remains unmet. The executable
        # validation below still rejects a rule that crossed before activation.
        distance_percent = None
        distance_atr = None
        distance = abs(directive.threshold - current)
        distance_percent = distance / current * 100 if current else None
        if atr1 is not None and atr1 > 0:
            distance_atr = distance / Decimal(str(atr1))
        confirmations = tuple(
            confirmation.model_copy(
                update={
                    "current_value": current_values.get(
                        confirmation.metric, confirmation.current_value
                    )
                }
            )
            for confirmation in directive.confirmations
        )
        directives.append(
            directive.model_copy(
                update={
                    "current_value": current,
                    "distance_percent": distance_percent,
                    "distance_atr": distance_atr,
                    "confirmations": confirmations,
                }
            )
        )
    normalized = assessment.model_copy(
        update={"monitoring_directives": tuple(directives)}
    )
    _validate_reviewed_directives(
        normalized.monitoring_directives,
        bundle,
        normalized,
        allow_partial_consecutive_crossing=True,
    )
    return MonitoringActivationResult(
        assessment=normalized,
        bundle_generated_at=bundle.generated_at,
        source_snapshot_sha256=bundle.source_snapshot_sha256,
        current_values={
            metric.value: str(value)
            for metric, value in sorted(
                _monitoring_current_values(bundle).items(),
                key=lambda item: item[0].value,
            )
        },
        dropped_directives=(),
    )


def _is_target_monitoring_directive(directive: MonitoringDirective) -> bool:
    family_parts = directive.family_id.replace(":", ".").split(".")
    if any(part in {"target", "tp", "takeprofit", "take_profit"} for part in family_parts):
        return True
    reason = directive.reason.lower()
    return "take-profit" in reason or "take profit" in reason or any(
        marker in directive.reason for marker in ("止盈", "目标已", "目标价", "目标结构")
    )


def _augment_bundles(
    bundles: tuple[EvidenceBundle, ...],
    results: Sequence[AnalysisToolResult],
) -> tuple[EvidenceBundle, ...]:
    augmented = []
    for bundle in bundles:
        related = [
            result for result in results if result.symbol is None or result.symbol == bundle.symbol
        ]
        if not related:
            augmented.append(bundle)
            continue
        items = list(bundle.evidence_items)
        assessments = list(bundle.tool_assessments)
        known = {item.evidence_id for item in items}
        for result in related:
            new_items = [item for item in result.evidence_items if item.evidence_id not in known]
            items.extend(new_items)
            known.update(item.evidence_id for item in new_items)
            assessments.append(
                ToolAssessment(
                    tool=f"CODEX_TOOL:{result.tool}",
                    status=result.status,
                    version="host-registry-v1",
                    reason=(result.error or "host-managed read-only tool completed")[:500],
                    latency_ms=result.latency_ms,
                    evidence_ids=tuple(item.evidence_id for item in new_items),
                )
            )
        augmented.append(
            bundle.model_copy(
                update={
                    "evidence_items": tuple(items),
                    "tool_assessments": tuple(assessments),
                }
            )
        )
    return tuple(augmented)


def _monitoring_current_values(
    bundle: EvidenceBundle,
) -> dict[MonitoringMetric, Decimal]:
    values: dict[MonitoringMetric, Decimal] = {
        MonitoringMetric.LAST_PRICE: bundle.canonical_last.value,
        MonitoringMetric.MARK_PRICE: bundle.canonical_mark.value,
    }
    timeframe_metrics = {
        ".PA.1M": MonitoringMetric.COMPLETED_1M_CLOSE,
        ".PA.5M": MonitoringMetric.COMPLETED_5M_CLOSE,
        ".PA.15M": MonitoringMetric.COMPLETED_15M_CLOSE,
        ".PA.30M": MonitoringMetric.COMPLETED_30M_CLOSE,
        ".PA.1H": MonitoringMetric.COMPLETED_1H_CLOSE,
    }
    for suffix, metric in timeframe_metrics.items():
        item = _evidence_values(bundle, suffix)
        number = _number(item or {}, "latest_close")
        if number is not None:
            values[metric] = Decimal(str(number))
        if suffix == ".PA.1M":
            turnover = _number(item or {}, "latest_turnover")
            if turnover is not None:
                values[MonitoringMetric.TURNOVER_1M] = Decimal(str(turnover))
    ticker = _evidence_values(bundle, ".BYBIT.TICKER")
    spread = _number(ticker or {}, "spread_bps")
    if spread is not None:
        values[MonitoringMetric.SPREAD_BPS] = Decimal(str(spread))
    derivatives = _evidence_values(bundle, ".DERIVATIVES")
    for key, metric in (
        ("current_open_interest", MonitoringMetric.OPEN_INTEREST),
        ("funding_rate", MonitoringMetric.FUNDING_RATE),
    ):
        number = _number(derivatives or {}, key)
        if number is not None:
            values[metric] = Decimal(str(number))
    book = _evidence_values(bundle, ".MICRO.BOOK")
    imbalance = _number(book or {}, "imbalance_top5")
    if imbalance is not None:
        values[MonitoringMetric.ORDERBOOK_IMBALANCE_L5] = Decimal(str(imbalance))
    trades_30s = _evidence_values(bundle, ".MICRO.TRADES.30S")
    if trades_30s is not None and trades_30s.get("qualified") is True:
        trade_delta = _number(trades_30s, "signed_delta_notional")
        if trade_delta is not None:
            values[MonitoringMetric.TRADE_DELTA_30S] = Decimal(str(trade_delta))
    liquidation = _evidence_values(bundle, ".LIQUIDATIONS.1M")
    if liquidation is not None and liquidation.get("coverage_complete") is True:
        long_notional = _number(liquidation, "long_notional") or 0
        short_notional = _number(liquidation, "short_notional") or 0
        values[MonitoringMetric.LIQUIDATION_NOTIONAL_1M] = Decimal(
            str(long_notional + short_notional)
        )
    return values


def _normalize_reviewed_baselines(
    review: MonitoringRuleReview,
    bundle: EvidenceBundle,
) -> MonitoringRuleReview:
    """Use host-observed baselines while leaving model strategy values untouched."""

    current_values = _monitoring_current_values(bundle)
    atr1 = _number(_evidence_values(bundle, ".PA.1M") or {}, "atr_14")
    directives: list[MonitoringDirective] = []
    for directive in review.directives:
        current = current_values.get(directive.metric)
        distance_percent = None
        distance_atr = None
        if current is not None:
            distance = abs(directive.threshold - current)
            distance_percent = distance / current * 100 if current else None
            if atr1 is not None and atr1 > 0:
                distance_atr = distance / Decimal(str(atr1))
        confirmations = tuple(
            confirmation.model_copy(
                update={
                    "current_value": current_values.get(
                        confirmation.metric, confirmation.current_value
                    )
                }
            )
            for confirmation in directive.confirmations
        )
        directives.append(
            directive.model_copy(
                update={
                    "current_value": current,
                    "distance_percent": distance_percent,
                    "distance_atr": distance_atr,
                    "valid_for_seconds": min(
                        max(directive.valid_for_seconds, 1800), 3600
                    ),
                    "confirmations": confirmations,
                }
            )
        )
    return review.model_copy(update={"directives": tuple(directives)})


def _validate_reviewed_directives(
    directives: Sequence[MonitoringDirective],
    bundle: EvidenceBundle,
    assessment: CandidateAssessment,
    *,
    allow_partial_consecutive_crossing: bool = False,
) -> None:
    if len(directives) > 3:
        raise ValueError(f"{bundle.symbol} monitoring review exceeds three rules")
    known = {item.evidence_id for item in bundle.evidence_items}
    current_values = _monitoring_current_values(bundle)
    family_ids: set[str] = set()
    semantic_keys: set[tuple[MonitoringMetric, Comparator, Decimal]] = set()
    for directive in directives:
        if directive.family_id in family_ids:
            raise ValueError(f"{bundle.symbol} duplicate monitoring family {directive.family_id}")
        family_ids.add(directive.family_id)
        if directive.metric not in _STRUCTURAL_MONITORING_METRICS:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} must use a completed-candle "
                "price structure as its primary condition"
            )
        if _is_target_monitoring_directive(directive):
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} cannot reference take-profit or target"
            )
        _validate_counter_direction_threat(directive, assessment, bundle.symbol)
        key = (directive.metric, directive.comparator, directive.threshold)
        if key in semantic_keys:
            raise ValueError(f"{bundle.symbol} contains duplicate monitoring expressions")
        semantic_keys.add(key)
        current = current_values.get(directive.metric)
        if current is None:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} primary metric has no current baseline"
            )
        if directive.current_value is None or not _baseline_matches(
            directive.current_value, current
        ):
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} primary baseline is stale or missing"
            )
        if _condition_met(current, directive.comparator, directive.threshold) and not (
            allow_partial_consecutive_crossing
            and directive.required_consecutive_observations > 1
        ):
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} complete primary condition is already met"
            )
        referenced = set(directive.evidence_ids)
        for confirmation in directive.confirmations:
            if confirmation.metric not in _SUPPORTED_MONITORING_METRICS:
                raise ValueError(
                    f"{bundle.symbol} {directive.family_id} confirmation metric is unsupported"
                )
            confirmation_current = current_values.get(confirmation.metric)
            if confirmation_current is None or not _baseline_matches(
                confirmation.current_value, confirmation_current
            ):
                raise ValueError(
                    f"{bundle.symbol} {directive.family_id} confirmation baseline is unavailable"
                )
            referenced.update(confirmation.evidence_ids)
        if not referenced <= known:
            raise ValueError(
                f"{bundle.symbol} {directive.family_id} references unknown evidence IDs"
            )


def _validate_counter_direction_threat(
    directive: MonitoringDirective,
    assessment: CandidateAssessment,
    symbol: str,
) -> None:
    direction = assessment.direction
    invalidation = assessment.invalidation
    if direction is None or invalidation is None:
        raise ValueError(f"{symbol} monitoring review lacks direction invalidation context")
    if direction is Direction.LONG_BIAS:
        if directive.comparator is not Comparator.LESS_THAN:
            raise ValueError(
                f"{symbol} {directive.family_id} is not a counter-direction LONG threat"
            )
        if directive.threshold < invalidation.reference_price:
            raise ValueError(
                f"{symbol} {directive.family_id} crosses beyond the formal LONG "
                "direction invalidation level"
            )
    else:
        if directive.comparator is not Comparator.GREATER_THAN:
            raise ValueError(
                f"{symbol} {directive.family_id} is not a counter-direction SHORT threat"
            )
        if directive.threshold > invalidation.reference_price:
            raise ValueError(
                f"{symbol} {directive.family_id} crosses beyond the formal SHORT "
                "direction invalidation level"
            )


def _baseline_matches(given: Decimal, actual: Decimal) -> bool:
    tolerance = max(abs(actual) * Decimal("0.001"), Decimal("0.00000001"))
    return abs(given - actual) <= tolerance


def _condition_met(value: Decimal, comparator: Comparator, threshold: Decimal) -> bool:
    if comparator is Comparator.GREATER_THAN:
        return value > threshold
    return value < threshold
