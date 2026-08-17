from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from bybit_signal.analysis.codex import CodexAnalysisError, CodexAnalyzer
from bybit_signal.cmi.adapter import CmiAdapter, CmiCaptureError
from bybit_signal.config import AppSettings
from bybit_signal.domain.enums import (
    Direction,
    PriceType,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandidateAssessment,
    CanonicalPrice,
    EvidenceBundle,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.evidence.builder import EvidenceBuilder
from bybit_signal.selection.scanner import BybitUniverseScanner
from bybit_signal.storage.sqlite import SignalStore


class SignalCycleService:
    def __init__(
        self,
        settings: AppSettings,
        *,
        scanner: BybitUniverseScanner,
        cmi: CmiAdapter,
        evidence_builder: EvidenceBuilder,
        analyzer: CodexAnalyzer,
        store: SignalStore,
    ) -> None:
        self._settings = settings
        self._scanner = scanner
        self._cmi = cmi
        self._evidence_builder = evidence_builder
        self._analyzer = analyzer
        self._store = store
        self._cycle_lock = asyncio.Lock()

    async def run_cycle(self) -> AnalysisCycleResult:
        async with self._cycle_lock:
            return await self._run_cycle_locked()

    async def _run_cycle_locked(self) -> AnalysisCycleResult:
        started_at = datetime.now(UTC)
        analysis_id = f"cycle_{started_at:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"
        await self._store.initialize()
        tracked_symbols = await self._store.previous_strong_symbols()
        previous = {
            symbol: conclusion
            for symbol in tracked_symbols
            if (conclusion := await self._store.latest_conclusion(symbol)) is not None
        }
        scan = await self._scanner.scan(limit=self._settings.analysis.top_candidates)
        candidate_symbols = tuple(candidate.symbol for candidate in scan.candidates)
        symbols = tuple(dict.fromkeys((*candidate_symbols, *tracked_symbols)))
        bundles, failures = await self._capture_evidence(symbols)

        assessments: dict[str, CandidateAssessment] = {}
        context_sha256 = self._fallback_context_hash(analysis_id, symbols, failures)
        model_diagnostics: dict[str, object] = {}
        if bundles:
            try:
                model_result = await self._analyzer.analyze(
                    analysis_id=analysis_id,
                    bundles=bundles,
                    tracked_symbols=tracked_symbols,
                )
                assessments = {
                    assessment.symbol: assessment
                    for assessment in model_result.response.assessments
                }
                context_sha256 = model_result.context_sha256
                model_diagnostics = {
                    "attempts": model_result.attempts,
                    "latency_ms": model_result.latency_ms,
                    "usage": model_result.usage,
                    "cycle_summary": model_result.response.cycle_summary,
                }
            except CodexAnalysisError as error:
                failures["CODEX"] = f"{error.code}: {error}"

        bundle_by_symbol = {bundle.symbol: bundle for bundle in bundles}
        candidate_price = {candidate.symbol: candidate.last_price for candidate in scan.candidates}
        conclusions: list[SignalConclusion] = []
        for symbol in symbols:
            bundle = bundle_by_symbol.get(symbol)
            assessment = assessments.get(symbol)
            if assessment is None:
                assessment = self._indeterminate_assessment(symbol, failures.get(symbol))
            canonical = self._fallback_price(
                symbol=symbol,
                bundle=bundle,
                candidate_price=candidate_price.get(symbol),
                previous=previous.get(symbol),
                now=started_at,
            )
            prior = previous.get(symbol)
            tracking_status, comparison = self._compare(prior, assessment, canonical)
            tools = (
                bundle.tool_assessments
                if bundle is not None
                else (
                    ToolAssessment(
                        tool="CMI",
                        status=ToolStatus.ERROR,
                        reason=failures.get(symbol, "market evidence unavailable"),
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

        completed_at = datetime.now(UTC)
        result = AnalysisCycleResult(
            analysis_id=analysis_id,
            started_at=started_at,
            completed_at=completed_at,
            candidate_symbols=candidate_symbols,
            tracked_symbols=tracked_symbols,
            conclusions=tuple(conclusions),
            strong_signal_count=sum(
                conclusion.assessment.strength is SignalStrength.STRONG
                for conclusion in conclusions
            ),
            context_sha256=context_sha256,
            diagnostics={
                "scan": scan.model_dump(mode="json"),
                "failures": failures,
                "model": model_diagnostics,
            },
        )
        await self._store.save_cycle(result, bundles)
        return result

    async def _capture_evidence(
        self,
        symbols: tuple[str, ...],
    ) -> tuple[tuple[EvidenceBundle, ...], dict[str, str]]:
        failures: dict[str, str] = {}

        async def capture(symbol: str) -> EvidenceBundle | None:
            try:
                snapshot = await self._cmi.capture(symbol)
                return self._evidence_builder.build(snapshot)
            except (CmiCaptureError, ValueError) as error:
                code = error.code if isinstance(error, CmiCaptureError) else type(error).__name__
                failures[symbol] = f"{code}: {error}"
                return None

        values = await asyncio.gather(*(capture(symbol) for symbol in symbols))
        return tuple(value for value in values if value is not None), failures

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
    def _compare(
        previous: SignalConclusion | None,
        current: CandidateAssessment,
        canonical: CanonicalPrice,
    ) -> tuple[TrackingStatus, str]:
        if previous is None:
            status = (
                TrackingStatus.NEW
                if current.strength is SignalStrength.STRONG
                else TrackingStatus.INDETERMINATE
            )
            return status, "没有上一轮已记录的强信号可比较。"
        prior = previous.assessment
        if prior.strength is not SignalStrength.STRONG:
            status = (
                TrackingStatus.NEW
                if current.strength is SignalStrength.STRONG
                else TrackingStatus.INDETERMINATE
            )
            return status, f"上一轮为 {prior.strength.value}, 本轮为 {current.strength.value}。"
        invalidated = SignalCycleService._prior_invalidated(previous, canonical.value)
        if current.strength is not SignalStrength.STRONG:
            status = TrackingStatus.INVALIDATED if invalidated else TrackingStatus.WEAKENED
            return (
                status,
                f"上一轮强信号本轮降为 {current.strength.value}; "
                + (
                    "价格已触及上一轮失效参考位。"
                    if invalidated
                    else "尚未确认触及上一轮失效参考位。"
                ),
            )
        if current.direction is not prior.direction:
            return TrackingStatus.INVALIDATED, "本轮强信号方向与上一轮相反, 上一轮方向失效。"
        if current.direction is None:
            raise ValueError("strong signal is missing its direction")
        prior_target = prior.take_profit.value if prior.take_profit is not None else None
        current_target = current.take_profit.value if current.take_profit is not None else None
        return (
            TrackingStatus.MAINTAINED,
            (
                f"强信号方向维持 {current.direction.value}; "
                f"目标由 {prior_target} 调整为 {current_target}。"
            ),
        )

    @staticmethod
    def _prior_invalidated(previous: SignalConclusion, price: Decimal) -> bool:
        invalidation = previous.assessment.invalidation
        direction = previous.assessment.direction
        if invalidation is None or direction is None:
            return False
        if direction is Direction.LONG_BIAS:
            return price <= invalidation.reference_price
        return price >= invalidation.reference_price

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
