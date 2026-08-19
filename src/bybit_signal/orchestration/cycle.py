from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from bybit_signal.analysis.codex import (
    CodexAnalysisError,
    CodexAnalyzer,
    revalidate_monitoring_assessment,
)
from bybit_signal.analysis.freshness import FreshnessGate
from bybit_signal.config import AppSettings
from bybit_signal.domain.enums import (
    CycleMode,
    CycleStatus,
    Direction,
    FreshnessDecision,
    MonitoringMetric,
    MonitoringReviewDecision,
    PriceType,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    AnalysisToolResult,
    CandidateAssessment,
    CanonicalPrice,
    EvidenceBundle,
    FreshnessAssessment,
    MonitoringDirective,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.evidence.builder import EvidenceBuilder
from bybit_signal.providers.deep_market import (
    BybitDeepMarketCollector,
    NativeMarketSnapshot,
)
from bybit_signal.selection.scanner import BybitUniverseScanner, RankedCandidate
from bybit_signal.storage.sqlite import SignalStore


class MonitoringActivationError(ValueError):
    def __init__(self, message: str, diagnostics: dict[str, object]) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MonitoringStageOutcome:
    cycle: AnalysisCycleResult
    status: str
    committed: bool
    errors: dict[str, str]
    diagnostics: dict[str, object]


class SignalCycleService:
    def __init__(
        self,
        settings: AppSettings,
        *,
        scanner: BybitUniverseScanner,
        market_collector: BybitDeepMarketCollector,
        evidence_builder: EvidenceBuilder,
        analyzer: CodexAnalyzer,
        store: SignalStore,
        freshness_gate: FreshnessGate | None = None,
    ) -> None:
        self._settings = settings
        self._scanner = scanner
        self._market_collector = market_collector
        self._evidence_builder = evidence_builder
        self._analyzer = analyzer
        self._store = store
        self._freshness_gate = freshness_gate
        self._scheduled_lock = asyncio.Lock()
        self._emergency_locks: dict[str, asyncio.Lock] = {}

    @property
    def scheduled_in_progress(self) -> bool:
        """Whether the authoritative half-hour market refresh currently owns the cycle."""

        return self._scheduled_lock.locked()

    async def run_cycle(self) -> AnalysisCycleResult:
        async with self._scheduled_lock:
            return await self._run_cycle_locked()

    async def run_emergency(
        self,
        symbol: str,
        reasons: tuple[str, ...],
        *,
        source_analysis_id: str | None = None,
    ) -> AnalysisCycleResult:
        lock = self._emergency_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            started_at = datetime.now(UTC)
            analysis_id = f"urgent_{started_at:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
            await self._store.initialize()
            prior = (
                await self._store.conclusion(source_analysis_id, symbol)
                if source_analysis_id is not None
                else await self._store.selected_scheduled_conclusion(symbol)
            )
            source_cycle = (
                await self._store.cycle(prior.analysis_id) if prior is not None else None
            )
            source_scheduled_analysis_id = (
                source_cycle.analysis_id
                if source_cycle is not None and source_cycle.mode is CycleMode.SCHEDULED
                else str(
                    (source_cycle.diagnostics if source_cycle is not None else {}).get(
                        "source_scheduled_analysis_id", ""
                    )
                )
            )
            bundles, failures = await self._capture_evidence((symbol,))
            bundle = bundles[0] if bundles else None
            assessment: CandidateAssessment | None = None
            context_sha256 = self._fallback_context_hash(analysis_id, (symbol,), failures)
            model_diagnostics: dict[str, object] = {}
            tool_results: tuple[AnalysisToolResult, ...] = ()
            freshness_checks: tuple[FreshnessAssessment, ...] = ()
            delivery_price: CanonicalPrice | None = None
            monitoring_activation: dict[str, object] = {}
            monitoring_candidates: dict[str, list[dict[str, object]]] = {}
            if prior is None:
                failures["ANALYSIS"] = "symbol is not an active scheduled primary signal"
            elif bundle is not None:
                try:
                    model_result = await self._analyzer.analyze(
                        analysis_id=analysis_id,
                        bundles=(bundle,),
                        tracked_symbols=(symbol,),
                        trigger_reasons=reasons,
                        mode=CycleMode.EMERGENCY,
                    )
                    assessment = model_result.response.assessments[0]
                    if model_result.evidence_bundles:
                        bundle = model_result.evidence_bundles[0]
                        bundles = (bundle,)
                    tool_results = model_result.tool_results
                    context_sha256 = model_result.context_sha256
                    model_diagnostics = {
                        "attempts": model_result.attempts,
                        "latency_ms": model_result.latency_ms,
                        "usage": model_result.usage,
                        "attempt_diagnostics": model_result.attempt_diagnostics,
                    }
                except CodexAnalysisError as error:
                    failures["CODEX"] = f"{error.code}: {error}"
                    model_diagnostics["attempt_diagnostics"] = error.diagnostics
            if assessment is not None and bundle is not None and self._freshness_gate is not None:
                try:
                    freshness_checks, refreshed = await self._freshness_gate.evaluate(
                        (assessment,), {symbol: bundle}
                    )
                    bundle = refreshed[symbol]
                    bundles = (bundle,)
                    if any(
                        check.decision is not FreshnessDecision.PASS for check in freshness_checks
                    ):
                        failures["FRESHNESS"] = "urgent review became stale before delivery"
                        assessment = None
                    else:
                        delivery_price = self._fresh_price(freshness_checks[0])
                except Exception as error:
                    failures["FRESHNESS"] = f"{type(error).__name__}: {error}"
                    assessment = None
            if assessment is not None:
                monitoring_candidates[symbol] = [
                    directive.model_dump(mode="json")
                    for directive in assessment.monitoring_directives
                ]
                assessment = assessment.model_copy(update={"monitoring_directives": ()})
                monitoring_activation = {
                    "status": (
                        "PENDING_REVIEW" if self._settings.monitoring.enabled else "DISABLED"
                    ),
                    "symbols": {},
                }
            if assessment is None:
                result = self._failed_cycle(
                    analysis_id=analysis_id,
                    mode=CycleMode.EMERGENCY,
                    started_at=started_at,
                    candidate_symbols=(symbol,),
                    tracked_symbols=(symbol,) if prior is not None else (),
                    context_sha256=context_sha256,
                    diagnostics={
                        "trigger_reasons": reasons,
                        "failures": failures,
                        "model": model_diagnostics,
                        "monitoring_activation": monitoring_activation,
                        "monitoring_candidates": monitoring_candidates,
                        "source_analysis_id": prior.analysis_id if prior is not None else None,
                        "source_scheduled_analysis_id": source_scheduled_analysis_id or None,
                    },
                )
                await self._store.save_cycle(result, bundles)
                await self._store.save_tool_results(analysis_id, tool_results)
                await self._store.save_freshness_checks(analysis_id, freshness_checks)
                return result
            canonical = delivery_price or self._fallback_price(
                symbol=symbol,
                bundle=bundle,
                candidate_price=None,
                previous=prior,
                now=started_at,
            )
            if prior is None:
                raise ValueError("emergency assessment lacks an active scheduled signal")
            tracking_status, comparison = self._compare_emergency(
                prior,
                assessment,
                bundle,
            )
            tools = (
                bundle.tool_assessments
                if bundle is not None
                else (
                    ToolAssessment(
                        tool="BYBIT_NATIVE_MARKET_DATA",
                        status=ToolStatus.ERROR,
                        reason=self._tool_reason(
                            failures.get(symbol, "market evidence unavailable")
                        ),
                    ),
                )
            )
            conclusion = SignalConclusion(
                analysis_id=analysis_id,
                generated_at=datetime.now(UTC),
                canonical_price=canonical,
                assessment=assessment,
                tracking_status=tracking_status,
                comparison_with_previous=comparison,
                tool_assessments=tools,
            )
            completed_at = datetime.now(UTC)
            result = AnalysisCycleResult(
                selection_contract_version=4,
                analysis_id=analysis_id,
                mode=CycleMode.EMERGENCY,
                status=CycleStatus.SUCCESS,
                started_at=started_at,
                completed_at=completed_at,
                candidate_symbols=(symbol,),
                tracked_symbols=(symbol,) if prior is not None else (),
                conclusions=(conclusion,),
                strong_signal_count=int(assessment.strength is SignalStrength.STRONG),
                selected_symbols=(),
                selected_signal_count=0,
                context_sha256=context_sha256,
                diagnostics={
                    "trigger_reasons": reasons,
                    "failures": failures,
                    "model": model_diagnostics,
                    "freshness": [check.model_dump(mode="json") for check in freshness_checks],
                    "monitoring_activation": monitoring_activation,
                    "monitoring_candidates": monitoring_candidates,
                    "source_analysis_id": prior.analysis_id,
                    "source_scheduled_analysis_id": source_scheduled_analysis_id,
                },
            )
            await self._store.save_cycle(result, bundles)
            await self._store.save_tool_results(analysis_id, tool_results)
            await self._store.save_freshness_checks(analysis_id, freshness_checks)
            return result

    async def _run_cycle_locked(self) -> AnalysisCycleResult:
        started_at = datetime.now(UTC)
        analysis_id = f"cycle_{started_at:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
        await self._store.initialize()
        previous_selected = await self._store.previous_scheduled_selected_conclusions()
        previous = {conclusion.assessment.symbol: conclusion for conclusion in previous_selected}
        tracked_symbols = tuple(previous)
        scan = await self._scanner.scan(limit=self._settings.analysis.top_candidates)
        candidate_symbols = tuple(candidate.symbol for candidate in scan.candidates)
        symbols = tuple(dict.fromkeys((*candidate_symbols, *tracked_symbols)))
        candidate_map = {candidate.symbol: candidate for candidate in scan.candidates}
        bundles, failures = await self._capture_evidence(symbols, candidate_map=candidate_map)
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        candidate_bundles = tuple(
            bundle_by_symbol[symbol] for symbol in candidate_symbols if symbol in bundle_by_symbol
        )
        assessments: dict[str, CandidateAssessment] = {}
        context_sha256 = self._fallback_context_hash(analysis_id, symbols, failures)
        model_diagnostics: dict[str, object] = {}
        tool_results: list[AnalysisToolResult] = []
        freshness_checks: tuple[FreshnessAssessment, ...] = ()
        repair_attempts = 0
        monitoring_activation: dict[str, object] = {}
        monitoring_candidates: dict[str, list[dict[str, object]]] = {}

        if len(candidate_bundles) == self._settings.analysis.top_candidates:
            try:
                model_result = await self._analyzer.analyze(
                    analysis_id=analysis_id,
                    bundles=candidate_bundles,
                    tracked_symbols=(),
                    mode=CycleMode.SCHEDULED,
                )
                assessments = {
                    assessment.symbol: assessment
                    for assessment in model_result.response.assessments
                }
                for bundle in model_result.evidence_bundles:
                    bundle_by_symbol[bundle.symbol] = bundle
                tool_results.extend(model_result.tool_results)
                context_sha256 = model_result.context_sha256
                model_diagnostics = {
                    "attempts": model_result.attempts,
                    "latency_ms": model_result.latency_ms,
                    "usage": model_result.usage,
                    "attempt_diagnostics": model_result.attempt_diagnostics,
                    "cycle_summary": model_result.response.cycle_summary,
                }
            except CodexAnalysisError as error:
                failures["CODEX"] = f"{error.code}: {error}"
                model_diagnostics["attempt_diagnostics"] = error.diagnostics
        else:
            failures["ANALYSIS"] = (
                "scheduled analysis requires five validated Top-5 evidence bundles"
            )

        if assessments:
            await self._review_dropped_symbols(
                analysis_id=analysis_id,
                candidate_symbols=candidate_symbols,
                tracked_symbols=tracked_symbols,
                previous=previous,
                bundle_by_symbol=bundle_by_symbol,
                assessments=assessments,
                failures=failures,
                tool_results=tool_results,
            )

        if assessments and self._freshness_gate is not None:
            try:
                (
                    assessments,
                    bundle_by_symbol,
                    freshness_checks,
                    repair_attempts,
                    context_sha256,
                ) = await self._apply_scheduled_freshness(
                    analysis_id=analysis_id,
                    assessments=assessments,
                    bundle_by_symbol=bundle_by_symbol,
                    candidate_symbols=candidate_symbols,
                    candidate_map=candidate_map,
                    failures=failures,
                    tool_results=tool_results,
                    context_sha256=context_sha256,
                    model_diagnostics=model_diagnostics,
                )
            except CodexAnalysisError as error:
                failures["FRESHNESS.CODEX"] = f"{error.code}: {error}"
                model_diagnostics["repair_error"] = {
                    "code": error.code,
                    "message": str(error),
                    "attempt_diagnostics": error.diagnostics,
                }
                repair_attempts = 1
                assessments = {}
            except Exception as error:
                failures["FRESHNESS"] = f"{type(error).__name__}: {error}"
                assessments = {}

        if assessments:
            for symbol, assessment in tuple(assessments.items()):
                if assessment.selection_rank is None:
                    continue
                monitoring_candidates[symbol] = [
                    directive.model_dump(mode="json")
                    for directive in assessment.monitoring_directives
                ]
                assessments[symbol] = assessment.model_copy(
                    update={"monitoring_directives": ()}
                )
            monitoring_activation = {
                "status": (
                    "PENDING_REVIEW" if self._settings.monitoring.enabled else "DISABLED"
                ),
                "symbols": {},
            }

        if not assessments:
            result = self._failed_cycle(
                analysis_id=analysis_id,
                mode=CycleMode.SCHEDULED,
                started_at=started_at,
                candidate_symbols=candidate_symbols,
                tracked_symbols=tracked_symbols,
                context_sha256=context_sha256,
                diagnostics={
                    "scan": scan.model_dump(mode="json"),
                    "failures": failures,
                    "model": model_diagnostics,
                    "freshness": [check.model_dump(mode="json") for check in freshness_checks],
                    "repair_attempts": repair_attempts,
                    "monitoring_activation": monitoring_activation,
                    "monitoring_candidates": monitoring_candidates,
                },
            )
            await self._store.save_cycle(result, tuple(bundle_by_symbol.values()))
            await self._store.save_tool_results(analysis_id, tuple(tool_results))
            await self._store.save_freshness_checks(analysis_id, freshness_checks)
            return result

        candidate_price = {candidate.symbol: candidate.last_price for candidate in scan.candidates}
        conclusions = self._conclusions(
            analysis_id=analysis_id,
            symbols=symbols,
            started_at=started_at,
            previous=previous,
            assessments=assessments,
            bundle_by_symbol=bundle_by_symbol,
            candidate_price=candidate_price,
            failures=failures,
            delivery_prices={
                **{check.symbol: self._fresh_price(check) for check in freshness_checks},
            },
        )
        selected = sorted(
            (
                conclusion
                for conclusion in conclusions
                if conclusion.assessment.selection_rank is not None
            ),
            key=lambda conclusion: conclusion.assessment.selection_rank or 0,
        )
        result = AnalysisCycleResult(
            selection_contract_version=4,
            analysis_id=analysis_id,
            mode=CycleMode.SCHEDULED,
            status=CycleStatus.SUCCESS,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            candidate_symbols=candidate_symbols,
            tracked_symbols=tracked_symbols,
            conclusions=conclusions,
            strong_signal_count=sum(
                conclusion.assessment.strength is SignalStrength.STRONG
                for conclusion in conclusions
            ),
            selected_symbols=tuple(conclusion.assessment.symbol for conclusion in selected),
            selected_signal_count=len(selected),
            context_sha256=context_sha256,
            diagnostics={
                "scan": scan.model_dump(mode="json"),
                "failures": failures,
                "model": model_diagnostics,
                "freshness": [check.model_dump(mode="json") for check in freshness_checks],
                "repair_attempts": repair_attempts,
                "monitoring_activation": monitoring_activation,
                "monitoring_candidates": monitoring_candidates,
                "strategy_version": self._settings.analysis.strategy_version,
                "prompt_version": self._settings.analysis.prompt_version,
            },
        )
        await self._store.save_cycle(
            result,
            tuple(bundle_by_symbol[symbol] for symbol in symbols if symbol in bundle_by_symbol),
        )
        await self._store.save_tool_results(analysis_id, tuple(tool_results))
        await self._store.save_freshness_checks(analysis_id, freshness_checks)
        return result

    async def review_and_activate_monitoring(
        self,
        cycle: AnalysisCycleResult,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> MonitoringStageOutcome:
        """Run the independent model rule review after the signal has been saved/delivered."""

        if not self._settings.monitoring.enabled:
            return MonitoringStageOutcome(
                cycle=cycle,
                status="DISABLED",
                committed=False,
                errors={},
                diagnostics={"status": "DISABLED", "symbols": {}},
            )
        visible = tuple(
            conclusion
            for conclusion in cycle.conclusions
            if conclusion.assessment.selection_rank is not None
            or cycle.mode is CycleMode.EMERGENCY
        )
        if symbols is not None:
            requested = frozenset(symbols)
            visible = tuple(
                conclusion
                for conclusion in visible
                if conclusion.assessment.symbol in requested
            )
        if not visible:
            return MonitoringStageOutcome(
                cycle=cycle,
                status="NOT_APPLICABLE",
                committed=False,
                errors={},
                diagnostics={"status": "NOT_APPLICABLE", "symbols": {}},
            )
        candidates_value = cycle.diagnostics.get("monitoring_candidates", {})
        candidates = candidates_value if isinstance(candidates_value, dict) else {}
        assessments: list[CandidateAssessment] = []
        candidate_errors: dict[str, str] = {}
        for conclusion in visible:
            symbol = conclusion.assessment.symbol
            raw = candidates.get(symbol, [])
            try:
                directives = tuple(
                    MonitoringDirective.model_validate(item)
                    for item in raw
                    if isinstance(item, dict)
                )
            except Exception as error:
                candidate_errors[symbol] = (
                    f"candidate contract invalid: {type(error).__name__}: {error}"
                )
                directives = ()
            assessments.append(
                conclusion.assessment.model_copy(
                    update={"monitoring_directives": directives}
                )
            )
        symbols = tuple(assessment.symbol for assessment in assessments)
        fresh_bundles, collection_failures = await self._capture_evidence(symbols)
        bundle_by_symbol = {bundle.symbol: bundle for bundle in fresh_bundles}
        missing = [symbol for symbol in symbols if symbol not in bundle_by_symbol]
        if missing:
            errors = {
                symbol: collection_failures.get(
                    symbol, "fresh monitoring-review evidence unavailable"
                )
                for symbol in missing
            }
            errors.update(candidate_errors)
            return MonitoringStageOutcome(
                cycle=cycle,
                status="FAILED",
                committed=False,
                errors=errors,
                diagnostics={
                    "status": "FAILED",
                    "step": "MONITORING_REVIEW_COLLECTION",
                    "collection_failures": collection_failures,
                    "symbols": {},
                },
            )
        try:
            model_review = await self._analyzer.review_monitoring(
                analysis_id=cycle.analysis_id,
                assessments=assessments,
                bundles=fresh_bundles,
            )
        except Exception as error:
            errors = {
                symbol: f"{type(error).__name__}: {error}"
                for symbol in symbols
            }
            errors.update(candidate_errors)
            return MonitoringStageOutcome(
                cycle=cycle,
                status="FAILED",
                committed=False,
                errors=errors,
                diagnostics={
                    "status": "FAILED",
                    "step": "MODEL_MONITORING_REVIEW",
                    "error": f"{type(error).__name__}: {error}",
                    "symbols": {},
                },
            )
        assessment_by_symbol = {assessment.symbol: assessment for assessment in assessments}
        reviewed: dict[str, CandidateAssessment] = {}
        review_diagnostics: dict[str, object] = {}
        errors = dict(candidate_errors)
        for review in model_review.response.reviews:
            review_diagnostics[review.symbol] = {
                "decision": review.decision.value,
                "rationale": review.rationale,
                "directive_count": len(review.directives),
            }
            if review.decision is MonitoringReviewDecision.REJECTED:
                reviewed[review.symbol] = assessment_by_symbol[review.symbol].model_copy(
                    update={"monitoring_directives": ()}
                )
            else:
                reviewed[review.symbol] = assessment_by_symbol[review.symbol].model_copy(
                    update={"monitoring_directives": review.directives}
                )
        accepted_symbols = tuple(
            symbol
            for symbol, assessment in reviewed.items()
            if assessment.monitoring_directives
        )
        activation_diagnostics: dict[str, object] = {
            "status": "NOT_REQUIRED" if not accepted_symbols else "READY",
            "symbols": {},
        }
        activation_prices: dict[str, CanonicalPrice] = {}
        activation_errors: dict[str, str] = {}
        if accepted_symbols:
            try:
                activated, activation_diagnostics, activation_prices = (
                    await self._activate_monitoring(reviewed, accepted_symbols)
                )
                reviewed.update(activated)
                raw_errors = activation_diagnostics.get("activation_errors", {})
                if isinstance(raw_errors, dict):
                    activation_errors = {
                        str(key): str(value) for key, value in raw_errors.items()
                    }
            except Exception as error:
                activation_diagnostics = {
                    "status": "FAILED",
                    "step": "MECHANICAL_ACTIVATION",
                    "error": f"{type(error).__name__}: {error}",
                    "symbols": {},
                }
                activation_errors = {
                    symbol: f"{type(error).__name__}: {error}"
                    for symbol in accepted_symbols
                }
                reviewed = {
                    symbol: assessment.model_copy(update={"monitoring_directives": ()})
                    for symbol, assessment in reviewed.items()
                }
        if activation_errors:
            initial_activation_errors = dict(activation_errors)
            (
                activation_repairs,
                activation_errors,
                activation_repair_diagnostics,
                repair_prices,
            ) = await self._repair_monitoring_activation(
                analysis_id=cycle.analysis_id,
                assessments={
                    symbol: assessment_by_symbol[symbol].model_copy(
                        update={
                            "monitoring_directives": next(
                                review.directives
                                for review in model_review.response.reviews
                                if review.symbol == symbol
                            )
                        }
                    )
                    for symbol in activation_errors
                },
                initial_errors=activation_errors,
            )
            reviewed.update(activation_repairs)
            activation_prices.update(repair_prices)
            activation_diagnostics = {
                **{
                    key: value
                    for key, value in activation_diagnostics.items()
                    if key not in {"activation_errors", "status"}
                },
                "status": "PARTIAL" if activation_errors else "READY",
                "initial_activation_errors": initial_activation_errors,
                "repair": activation_repair_diagnostics,
            }
            if activation_errors:
                activation_diagnostics["activation_errors"] = activation_errors
        errors.update(activation_errors)
        conclusions = tuple(
            conclusion.model_copy(
                update={
                    "canonical_price": activation_prices.get(
                        conclusion.assessment.symbol, conclusion.canonical_price
                    ),
                    "assessment": reviewed.get(
                        conclusion.assessment.symbol, conclusion.assessment
                    ),
                }
            )
            for conclusion in cycle.conclusions
        )
        monitoring_status = (
            "READY"
            if not errors
            else "PARTIAL"
            if any(
                conclusion.assessment.monitoring_directives for conclusion in conclusions
            )
            else "FAILED"
        )
        diagnostics = {
            **cycle.diagnostics,
            "monitoring_review": {
                "status": monitoring_status,
                "context_sha256": model_review.context_sha256,
                "latency_ms": model_review.latency_ms,
                "attempts": model_review.attempts,
                "attempt_diagnostics": model_review.attempt_diagnostics,
                "repair_diagnostics": model_review.repair_diagnostics,
                "usage": model_review.usage,
                "symbols": review_diagnostics,
            },
            "monitoring_activation": activation_diagnostics,
        }
        updated_cycle = cycle.model_copy(
            update={"conclusions": conclusions, "diagnostics": diagnostics}
        )
        authoritative_scheduled_analysis_id = (
            cycle.analysis_id
            if cycle.mode is CycleMode.SCHEDULED
            else str(cycle.diagnostics.get("source_scheduled_analysis_id", ""))
        )
        committed = await self._store.commit_monitoring_version(
            updated_cycle,
            authoritative_scheduled_analysis_id=authoritative_scheduled_analysis_id,
        )
        if not committed:
            errors["VERSION"] = (
                "newer scheduled or emergency analysis superseded this monitoring review"
            )
            monitoring_status = "SUPERSEDED"
        return MonitoringStageOutcome(
            cycle=updated_cycle,
            status=monitoring_status,
            committed=committed,
            errors=errors,
            diagnostics={
                "status": monitoring_status,
                "review": diagnostics["monitoring_review"],
                "activation": activation_diagnostics,
            },
        )

    async def _repair_monitoring_activation(
        self,
        *,
        analysis_id: str,
        assessments: dict[str, CandidateAssessment],
        initial_errors: dict[str, str],
    ) -> tuple[
        dict[str, CandidateAssessment],
        dict[str, str],
        dict[str, object],
        dict[str, CanonicalPrice],
    ]:
        """Silently re-collect and re-review only symbols that failed activation."""

        maximum_repairs = self._settings.analysis.monitoring_review_repair_attempts
        current = dict(assessments)
        pending = dict(initial_errors)
        histories: dict[str, list[str]] = {
            symbol: [reason] for symbol, reason in pending.items()
        }
        attempt_counts = {symbol: 0 for symbol in pending}
        repaired: dict[str, CandidateAssessment] = {}
        declined: set[str] = set()
        activation_prices: dict[str, CanonicalPrice] = {}
        rounds: list[dict[str, object]] = []
        permanent_codes = {
            "CODEX_CREDITS_EXHAUSTED",
            "CODEX_AUTH_REQUIRED",
            "CODEX_MODEL_UNAVAILABLE",
        }
        for repair_attempt in range(1, maximum_repairs + 1):
            symbols = tuple(pending)
            if not symbols:
                break
            for symbol in symbols:
                attempt_counts[symbol] += 1
            bundles, collection_failures = await self._capture_evidence(symbols)
            bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
            reviewable = tuple(
                symbol for symbol in symbols if symbol in bundle_by_symbol
            )
            next_errors: dict[str, str] = {
                symbol: collection_failures.get(
                    symbol, "fresh activation-repair evidence unavailable"
                )
                for symbol in symbols
                if symbol not in bundle_by_symbol
            }
            round_diagnostics: dict[str, object] = {
                "repair_attempt": repair_attempt,
                "symbols": symbols,
                "collection_failures": collection_failures,
            }
            stop_repairs = False
            if reviewable:
                try:
                    model_repair = await self._analyzer.review_monitoring(
                        analysis_id=analysis_id,
                        assessments=tuple(current[symbol] for symbol in reviewable),
                        bundles=tuple(bundle_by_symbol[symbol] for symbol in reviewable),
                        repair_reasons={
                            symbol: tuple(histories[symbol]) for symbol in reviewable
                        },
                        maximum_repair_attempts=0,
                    )
                except CodexAnalysisError as error:
                    if error.code in permanent_codes:
                        stop_repairs = True
                    message = f"{error.code}: {error}"
                    next_errors.update({symbol: message for symbol in reviewable})
                    round_diagnostics["model_error"] = message
                    round_diagnostics["attempt_diagnostics"] = error.diagnostics
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    next_errors.update({symbol: message for symbol in reviewable})
                    round_diagnostics["model_error"] = message
                else:
                    round_diagnostics["model_review"] = {
                        "context_sha256": model_repair.context_sha256,
                        "latency_ms": model_repair.latency_ms,
                        "attempts": model_repair.attempts,
                        "attempt_diagnostics": model_repair.attempt_diagnostics,
                        "repair_diagnostics": model_repair.repair_diagnostics,
                        "usage": model_repair.usage,
                    }
                    accepted: dict[str, CandidateAssessment] = {}
                    for review in model_repair.response.reviews:
                        symbol = review.symbol
                        if review.decision is MonitoringReviewDecision.REJECTED:
                            repaired[symbol] = current[symbol].model_copy(
                                update={"monitoring_directives": ()}
                            )
                            declined.add(symbol)
                            continue
                        candidate = current[symbol].model_copy(
                            update={"monitoring_directives": review.directives}
                        )
                        current[symbol] = candidate
                        accepted[symbol] = candidate
                    if accepted:
                        activated, activation_diag, prices = await self._activate_monitoring(
                            accepted, tuple(accepted)
                        )
                        round_diagnostics["activation"] = activation_diag
                        raw_activation_errors = activation_diag.get(
                            "activation_errors", {}
                        )
                        activation_error_map = (
                            {
                                str(key): str(value)
                                for key, value in raw_activation_errors.items()
                            }
                            if isinstance(raw_activation_errors, dict)
                            else {}
                        )
                        for symbol, assessment in activated.items():
                            if assessment.monitoring_directives:
                                repaired[symbol] = assessment
                                activation_prices[symbol] = prices[symbol]
                            else:
                                next_errors[symbol] = activation_error_map.get(
                                    symbol,
                                    "no monitoring directive remained after repair activation",
                                )
            rounds.append(round_diagnostics)
            for symbol, reason in next_errors.items():
                histories[symbol].append(reason)
            pending = {
                symbol: next_errors[symbol]
                for symbol in symbols
                if symbol in next_errors
            }
            if stop_repairs:
                break

        for symbol in pending:
            repaired[symbol] = current[symbol].model_copy(
                update={"monitoring_directives": ()}
            )
        diagnostics: dict[str, object] = {
            "status": "EXHAUSTED" if pending else "READY",
            "maximum_repair_attempts": maximum_repairs,
            "rounds": rounds,
            "symbols": {
                symbol: {
                    "attempts": attempt_counts[symbol],
                    "history": histories[symbol],
                    "final_status": (
                        "FAILED"
                        if symbol in pending
                        else "NO_QUALIFIED_RULE"
                        if symbol in declined
                        else "READY"
                    ),
                }
                for symbol in initial_errors
            },
        }
        return repaired, pending, diagnostics, activation_prices

    async def _apply_scheduled_freshness(
        self,
        *,
        analysis_id: str,
        assessments: dict[str, CandidateAssessment],
        bundle_by_symbol: dict[str, EvidenceBundle],
        candidate_symbols: tuple[str, ...],
        candidate_map: dict[str, RankedCandidate],
        failures: dict[str, str],
        tool_results: list[AnalysisToolResult],
        context_sha256: str,
        model_diagnostics: dict[str, object],
    ) -> tuple[
        dict[str, CandidateAssessment],
        dict[str, EvidenceBundle],
        tuple[FreshnessAssessment, ...],
        int,
        str,
    ]:
        if self._freshness_gate is None:
            return assessments, bundle_by_symbol, (), 0, context_sha256
        selected = sorted(
            (
                assessment
                for assessment in assessments.values()
                if assessment.selection_rank is not None
            ),
            key=lambda assessment: assessment.selection_rank or 0,
        )
        checks, refreshed = await self._freshness_gate.evaluate(selected, bundle_by_symbol)
        bundle_by_symbol.update(refreshed)
        if all(check.decision is FreshnessDecision.PASS for check in checks):
            return assessments, bundle_by_symbol, checks, 0, context_sha256
        model_diagnostics["freshness_repair_trigger"] = [
            check.model_dump(mode="json") for check in checks
        ]
        repaired_snapshots, repair_failures = await self._market_collector.collect_many(
            candidate_symbols
        )
        failures.update({f"REPAIR.{key}": value for key, value in repair_failures.items()})
        repaired_bundles = tuple(
            self._build_evidence(snapshot, candidate_map.get(symbol))
            for symbol in candidate_symbols
            if (snapshot := repaired_snapshots.get(symbol)) is not None
        )
        if len(repaired_bundles) != self._settings.analysis.top_candidates:
            failures["FRESHNESS_REPAIR"] = "repair requires five refreshed Top-5 evidence bundles"
            return {}, bundle_by_symbol, checks, 1, context_sha256
        repair_result = await self._analyzer.analyze(
            analysis_id=analysis_id,
            bundles=repaired_bundles,
            tracked_symbols=(),
            mode=CycleMode.SCHEDULED,
        )
        model_diagnostics["repair"] = {
            "attempts": repair_result.attempts,
            "latency_ms": repair_result.latency_ms,
            "usage": repair_result.usage,
            "attempt_diagnostics": repair_result.attempt_diagnostics,
            "cycle_summary": repair_result.response.cycle_summary,
        }
        repaired_assessments = {
            assessment.symbol: assessment for assessment in repair_result.response.assessments
        }
        repaired_assessments.update(
            {
                symbol: assessment
                for symbol, assessment in assessments.items()
                if symbol not in candidate_symbols
            }
        )
        final_bundles = repair_result.evidence_bundles or repaired_bundles
        bundle_by_symbol.update({bundle.symbol: bundle for bundle in final_bundles})
        tool_results.extend(_namespace_tools(repair_result.tool_results, "repair"))
        selected = sorted(
            (
                assessment
                for assessment in repaired_assessments.values()
                if assessment.selection_rank is not None
            ),
            key=lambda assessment: assessment.selection_rank or 0,
        )
        checks, refreshed = await self._freshness_gate.evaluate(selected, bundle_by_symbol)
        bundle_by_symbol.update(refreshed)
        if any(check.decision is not FreshnessDecision.PASS for check in checks):
            failures["FRESHNESS_REPAIR"] = "repair analysis was still stale or structurally invalid"
            return {}, bundle_by_symbol, checks, 1, repair_result.context_sha256
        return (
            repaired_assessments,
            bundle_by_symbol,
            checks,
            1,
            repair_result.context_sha256,
        )

    async def _activate_monitoring(
        self,
        assessments: dict[str, CandidateAssessment],
        symbols: tuple[str, ...],
    ) -> tuple[
        dict[str, CandidateAssessment],
        dict[str, object],
        dict[str, CanonicalPrice],
    ]:
        if not symbols:
            raise ValueError("visible conclusions require monitoring activation symbols")
        bundles, failures = await self._capture_evidence(symbols)
        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        missing = [symbol for symbol in symbols if symbol not in bundle_by_symbol]
        updated = dict(assessments)
        diagnostics: dict[str, object] = {
            "status": "READY",
            "collection_failures": failures,
            "symbols": {},
        }
        symbol_diagnostics: dict[str, object] = {}
        activation_errors: dict[str, str] = {
            symbol: failures.get(symbol, "fresh activation evidence unavailable")
            for symbol in missing
        }
        activation_prices: dict[str, CanonicalPrice] = {}
        for symbol in symbols:
            original = assessments[symbol]
            bundle = bundle_by_symbol.get(symbol)
            if bundle is None:
                updated[symbol] = original.model_copy(update={"monitoring_directives": ()})
                continue
            try:
                activation = revalidate_monitoring_assessment(
                    original,
                    bundle,
                    self._settings.monitoring,
                )
            except Exception as error:
                activation_errors[symbol] = f"{type(error).__name__}: {error}"
                updated[symbol] = original.model_copy(update={"monitoring_directives": ()})
                continue
            activation_prices[symbol] = bundle.canonical_last
            remaining = activation.assessment.monitoring_directives
            symbol_diagnostics[symbol] = {
                "bundle_generated_at": activation.bundle_generated_at.isoformat(),
                "source_snapshot_sha256": activation.source_snapshot_sha256,
                "current_values": activation.current_values,
                "original_directive_count": len(original.monitoring_directives),
                "active_directive_count": len(remaining),
                "dropped_directives": activation.dropped_directives,
            }
            if not remaining:
                activation_errors[symbol] = (
                    "no monitoring directive remained after delivery-time rebaseline"
                )
                updated[symbol] = original.model_copy(update={"monitoring_directives": ()})
                continue
            updated[symbol] = activation.assessment
        diagnostics["symbols"] = symbol_diagnostics
        if activation_errors:
            diagnostics["status"] = "PARTIAL"
            diagnostics["activation_errors"] = activation_errors
        return updated, diagnostics, activation_prices

    async def _review_dropped_symbols(
        self,
        *,
        analysis_id: str,
        candidate_symbols: tuple[str, ...],
        tracked_symbols: tuple[str, ...],
        previous: dict[str, SignalConclusion],
        bundle_by_symbol: dict[str, EvidenceBundle],
        assessments: dict[str, CandidateAssessment],
        failures: dict[str, str],
        tool_results: list[AnalysisToolResult],
    ) -> None:
        for symbol in tracked_symbols:
            if symbol in candidate_symbols:
                continue
            bundle = bundle_by_symbol.get(symbol)
            try:
                if bundle is None:
                    raise ValueError("tracking evidence is unavailable")
                tracking_result = await self._analyzer.analyze(
                    analysis_id=analysis_id,
                    bundles=(bundle,),
                    tracked_symbols=(symbol,),
                    mode=CycleMode.EMERGENCY,
                )
                current = tracking_result.response.assessments[0]
                assessments[symbol] = current.model_copy(
                    update={
                        "strength": (
                            SignalStrength.WATCH
                            if current.strength is SignalStrength.STRONG
                            else current.strength
                        ),
                        "selection_rank": None,
                        "confidence": None,
                        "monitoring_directives": (),
                        "forming_15m": None,
                        "forming_30m": None,
                        "forming_1h": None,
                        "next_15m": None,
                    }
                )
                if tracking_result.evidence_bundles:
                    bundle_by_symbol[symbol] = tracking_result.evidence_bundles[0]
                tool_results.extend(
                    _namespace_tools(tracking_result.tool_results, f"track_{symbol}")
                )
            except Exception as error:
                failures[f"TRACKING.{symbol}"] = f"{type(error).__name__}: {error}"
                prior = previous[symbol].assessment
                assessments[symbol] = prior.model_copy(
                    update={
                        "strength": SignalStrength.WATCH,
                        "selection_rank": None,
                        "confidence": None,
                        "monitoring_directives": (),
                        "forming_15m": None,
                        "forming_30m": None,
                        "forming_1h": None,
                        "next_15m": None,
                    }
                )

    def _conclusions(
        self,
        *,
        analysis_id: str,
        symbols: tuple[str, ...],
        started_at: datetime,
        previous: dict[str, SignalConclusion],
        assessments: dict[str, CandidateAssessment],
        bundle_by_symbol: dict[str, EvidenceBundle],
        candidate_price: dict[str, Decimal],
        failures: dict[str, str],
        delivery_prices: dict[str, CanonicalPrice],
    ) -> tuple[SignalConclusion, ...]:
        conclusions = []
        for symbol in symbols:
            bundle = bundle_by_symbol.get(symbol)
            assessment = assessments.get(symbol) or self._indeterminate_assessment(
                symbol, failures.get(symbol)
            )
            canonical = delivery_prices.get(symbol) or self._fallback_price(
                symbol=symbol,
                bundle=bundle,
                candidate_price=candidate_price.get(symbol),
                previous=previous.get(symbol),
                now=started_at,
            )
            tracking_status, comparison = self._compare(
                previous.get(symbol), assessment, bundle
            )
            tools = (
                bundle.tool_assessments
                if bundle is not None
                else (
                    ToolAssessment(
                        tool="BYBIT_NATIVE_MARKET_DATA",
                        status=ToolStatus.ERROR,
                        reason=self._tool_reason(
                            failures.get(symbol, "market evidence unavailable")
                        ),
                    ),
                )
            )
            conclusions.append(
                SignalConclusion(
                    analysis_id=analysis_id,
                    generated_at=datetime.now(UTC),
                    canonical_price=canonical,
                    assessment=assessment,
                    tracking_status=tracking_status,
                    comparison_with_previous=comparison,
                    tool_assessments=tools,
                )
            )
        return tuple(conclusions)

    @staticmethod
    def _failed_cycle(
        *,
        analysis_id: str,
        mode: CycleMode,
        started_at: datetime,
        candidate_symbols: tuple[str, ...],
        tracked_symbols: tuple[str, ...],
        context_sha256: str,
        diagnostics: dict[str, object],
    ) -> AnalysisCycleResult:
        return AnalysisCycleResult(
            selection_contract_version=4,
            analysis_id=analysis_id,
            mode=mode,
            status=CycleStatus.FAILED,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            candidate_symbols=candidate_symbols,
            tracked_symbols=tracked_symbols,
            conclusions=(),
            strong_signal_count=0,
            selected_symbols=(),
            selected_signal_count=0,
            context_sha256=context_sha256,
            diagnostics=diagnostics,
        )

    async def _capture_evidence(
        self,
        symbols: tuple[str, ...],
        *,
        candidate_map: dict[str, RankedCandidate] | None = None,
    ) -> tuple[tuple[EvidenceBundle, ...], dict[str, str]]:
        snapshots, failures = await self._market_collector.collect_many(symbols)
        bundles: list[EvidenceBundle] = []
        for symbol in symbols:
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            try:
                bundles.append(
                    self._build_evidence(
                        snapshot,
                        (candidate_map or {}).get(symbol),
                    )
                )
            except Exception as error:
                failures[symbol] = f"{type(error).__name__}: {error}"
        return tuple(bundles), failures

    def _build_evidence(
        self,
        snapshot: NativeMarketSnapshot,
        candidate: RankedCandidate | None,
    ) -> EvidenceBundle:
        if candidate is not None and isinstance(self._evidence_builder, EvidenceBuilder):
            return self._evidence_builder.build(snapshot, candidate)
        return self._evidence_builder.build(snapshot)

    @staticmethod
    def _indeterminate_assessment(
        symbol: str,
        failure: str | None,
    ) -> CandidateAssessment:
        reason = failure or "Codex did not return a validated assessment"
        return CandidateAssessment(
            symbol=symbol,
            strength=SignalStrength.INDETERMINATE,
            market_state="数据不可判定",
            summary="本轮数据或模型校验未通过, 不能形成可靠信号。",
            details=f"本轮已停止方向判断。原因: {reason}",
            uncertainties=(reason,),
        )

    @staticmethod
    def _fallback_price(
        *,
        symbol: str,
        bundle: EvidenceBundle | None,
        candidate_price: Decimal | None,
        previous: SignalConclusion | None,
        now: datetime,
    ) -> CanonicalPrice:
        if bundle is not None:
            return bundle.canonical_last
        if candidate_price is not None:
            return CanonicalPrice(
                symbol=symbol,
                price_type=PriceType.LAST,
                value=candidate_price,
                timestamp=now,
            )
        if previous is not None:
            return previous.canonical_price
        raise ValueError(f"no canonical or fallback price for {symbol}")

    @staticmethod
    def _fresh_price(check: FreshnessAssessment) -> CanonicalPrice:
        return CanonicalPrice(
            symbol=check.symbol,
            price_type=PriceType.LAST,
            value=check.latest_price,
            timestamp=check.checked_at,
        )

    @staticmethod
    def _compare(
        previous: SignalConclusion | None,
        current: CandidateAssessment,
        bundle: EvidenceBundle | None,
    ) -> tuple[TrackingStatus, str]:
        if previous is None:
            status = (
                TrackingStatus.NEW
                if current.selection_rank is not None
                else TrackingStatus.INDETERMINATE
            )
            return status, "没有上一轮已记录的主信号可比较。"
        prior = previous.assessment
        invalidated = SignalCycleService._prior_invalidated(previous, bundle)
        if current.selection_rank is None:
            if current.strength is SignalStrength.INDETERMINATE:
                return (
                    TrackingStatus.INDETERMINATE,
                    "上一轮主信号本轮数据不可判定, 已停止实时监测。",
                )
            status = TrackingStatus.INVALIDATED if invalidated else TrackingStatus.EXITED
            return (
                status,
                "上一轮主信号本轮未进入相对最优两个机会; "
                + (
                    "已完成K线收盘确认上一轮方向结构失效。"
                    if invalidated
                    else "尚未确认上一轮方向结构失效, 已停止实时监测。"
                ),
            )
        if current.direction is not prior.direction:
            return TrackingStatus.REVERSED, "本轮主信号方向与上一轮相反。"
        if current.direction is None:
            raise ValueError("selected signal is missing its direction")
        return (
            TrackingStatus.MAINTAINED,
            f"主信号方向维持 {current.direction.value}; 已按本轮结构更新方向失效条件。",
        )

    @staticmethod
    def _compare_emergency(
        previous: SignalConclusion,
        current: CandidateAssessment,
        bundle: EvidenceBundle | None,
    ) -> tuple[TrackingStatus, str]:
        if current.direction is not None and current.direction is not previous.assessment.direction:
            return TrackingStatus.REVERSED, "紧急复核方向已与定时主信号相反。"
        if current.direction is None:
            return (
                TrackingStatus.INDETERMINATE,
                "紧急复核未形成明确方向, 不能由宿主代替模型判定失效。",
            )
        return TrackingStatus.MAINTAINED, "阈值穿越后复核, 原主信号方向尚未失效。"

    @staticmethod
    def _prior_invalidated(
        previous: SignalConclusion,
        bundle: EvidenceBundle | None,
    ) -> bool:
        if bundle is None:
            return False
        invalidation = previous.assessment.invalidation
        direction = previous.assessment.direction
        if invalidation is None or direction is None:
            return False
        metric = _formal_invalidation_metric(previous.assessment)
        if metric is None:
            return False
        close = _bundle_completed_close(bundle, metric)
        if close is None:
            return False
        if direction is Direction.SHORT_BIAS:
            return close > invalidation.reference_price
        return close < invalidation.reference_price

    @staticmethod
    def _fallback_context_hash(
        analysis_id: str,
        symbols: tuple[str, ...],
        failures: dict[str, str],
    ) -> str:
        payload = json.dumps(
            {"analysis_id": analysis_id, "symbols": symbols, "failures": failures},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_reason(value: str) -> str:
        compact = value.replace("\r", " ").replace("\n", " ").strip()
        return compact if len(compact) <= 500 else compact[:497] + "..."


def _namespace_tools(
    results: tuple[AnalysisToolResult, ...],
    namespace: str,
) -> tuple[AnalysisToolResult, ...]:
    return tuple(
        result.model_copy(update={"request_id": f"{namespace}_{result.request_id}"})
        for result in results
    )


def _bundle_completed_close(
    bundle: EvidenceBundle,
    metric: MonitoringMetric,
) -> Decimal | None:
    suffixes = {
        MonitoringMetric.COMPLETED_1M_CLOSE: ".PA.1M",
        MonitoringMetric.COMPLETED_5M_CLOSE: ".PA.5M",
        MonitoringMetric.COMPLETED_15M_CLOSE: ".PA.15M",
        MonitoringMetric.COMPLETED_30M_CLOSE: ".PA.30M",
        MonitoringMetric.COMPLETED_1H_CLOSE: ".PA.1H",
    }
    suffix = suffixes.get(metric)
    if suffix is None:
        return None
    item = next(
        (item for item in bundle.evidence_items if item.evidence_id.endswith(suffix)),
        None,
    )
    if item is None:
        return None
    value = item.values.get("latest_close")
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _formal_invalidation_metric(
    assessment: CandidateAssessment,
) -> MonitoringMetric | None:
    invalidation = assessment.invalidation
    if invalidation is None:
        return None
    condition = invalidation.condition.lower().replace(" ", "")
    candidates = (
        (".PA.1M", "1m", MonitoringMetric.COMPLETED_1M_CLOSE),
        (".PA.5M", "5m", MonitoringMetric.COMPLETED_5M_CLOSE),
        (".PA.15M", "15m", MonitoringMetric.COMPLETED_15M_CLOSE),
        (".PA.30M", "30m", MonitoringMetric.COMPLETED_30M_CLOSE),
        (".PA.1H", "1h", MonitoringMetric.COMPLETED_1H_CLOSE),
    )
    cited = [
        (suffix, timeframe, metric)
        for suffix, timeframe, metric in candidates
        if any(evidence_id.endswith(suffix) for evidence_id in invalidation.evidence_ids)
    ]
    for _suffix, timeframe, metric in cited:
        if timeframe in condition or timeframe.replace("m", "分钟") in condition:
            return metric
    return cited[0][2] if cited else None
