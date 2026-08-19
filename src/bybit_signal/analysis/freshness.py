from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from bybit_signal.config import FreshnessConfig
from bybit_signal.domain.enums import (
    Comparator,
    Direction,
    FreshnessDecision,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    Candle,
    EvidenceBundle,
    FreshnessAssessment,
)
from bybit_signal.providers.bybit import BybitPublicClient, BybitTicker

_INVALIDATION_SUFFIXES: dict[str, str] = {
    ".PA.1M": "1m",
    ".PA.5M": "5m",
    ".PA.15M": "15m",
    ".PA.30M": "30m",
    ".PA.1H": "1h",
}


@dataclass(frozen=True, slots=True)
class _InvalidationCheck:
    timeframe: str
    comparator: Comparator
    threshold: Decimal


class FreshnessGate:
    """Recheck completed-candle direction invalidation after model latency."""

    def __init__(self, client: BybitPublicClient, config: FreshnessConfig) -> None:
        self._client = client
        self._config = config

    async def evaluate(
        self,
        assessments: Sequence[CandidateAssessment],
        bundles: Mapping[str, EvidenceBundle],
    ) -> tuple[
        tuple[FreshnessAssessment, ...],
        dict[str, EvidenceBundle],
    ]:
        tickers = await self._client.tickers()

        async def check(
            assessment: CandidateAssessment,
        ) -> tuple[FreshnessAssessment, EvidenceBundle]:
            bundle = bundles[assessment.symbol]
            ticker = tickers.get(assessment.symbol)
            if ticker is None or ticker.mark_price is None:
                raise ValueError(f"{assessment.symbol} has no fresh ticker")
            checks = _invalidation_checks(assessment)
            if not checks:
                raise ValueError(
                    f"{assessment.symbol} direction invalidation has no completed-candle "
                    "timeframe evidence"
                )
            timeframes = tuple(
                dict.fromkeys(check.timeframe for check in checks)
            )
            candle_results = await asyncio.gather(
                *(
                    self._client.completed_candles(
                        assessment.symbol,
                        timeframe=timeframe,  # type: ignore[arg-type]
                        limit=2,
                    )
                    for timeframe in timeframes
                )
            )
            latest_candles: dict[str, Candle] = {}
            for timeframe, candles in zip(timeframes, candle_results, strict=True):
                if not candles:
                    raise ValueError(
                        f"{assessment.symbol} has no completed {timeframe} candle for freshness"
                    )
                latest_candles[timeframe] = candles[-1]
            result = self._evaluate_one(
                assessment=assessment,
                bundle=bundle,
                ticker=ticker,
                checks=checks,
                latest_candles=latest_candles,
            )
            # The analyzed bundle remains immutable audit evidence. Delivery-time values
            # are recorded in FreshnessAssessment and activation evidence separately.
            return result, bundle

        checked = await asyncio.gather(*(check(assessment) for assessment in assessments))
        return (
            tuple(item[0] for item in checked),
            {item[1].symbol: item[1] for item in checked},
        )

    @staticmethod
    def _evaluate_one(
        *,
        assessment: CandidateAssessment,
        bundle: EvidenceBundle,
        ticker: BybitTicker,
        checks: Sequence[_InvalidationCheck],
        latest_candles: Mapping[str, Candle],
    ) -> FreshnessAssessment:
        now = datetime.now(UTC)
        original = bundle.canonical_last.value
        latest = ticker.last_price
        drift_percent = (latest / original - 1) * 100
        crossed: list[str] = []
        current: list[str] = []
        for check in checks:
            timeframe = check.timeframe
            candle = latest_candles[timeframe]
            is_crossed = (
                candle.close > check.threshold
                if check.comparator is Comparator.GREATER_THAN
                else candle.close < check.threshold
            )
            statement = (
                f"已完成{timeframe}收盘 {candle.close} "
                f"{'突破' if check.comparator is Comparator.GREATER_THAN else '跌破'} "
                f"方向失效位 {check.threshold}"
            )
            if is_crossed:
                crossed.append(statement)
            else:
                current.append(
                    f"已完成{timeframe}收盘 {candle.close} 尚未触及方向失效位 "
                    f"{check.threshold}"
                )
        decision = FreshnessDecision.REPAIR if crossed else FreshnessDecision.PASS
        reasons = tuple(crossed or current)
        age = (now - bundle.generated_at).total_seconds()
        if age > 0:
            reasons += (f"模型证据距复核约 {age:.0f} 秒, 只按完成柱结构判断方向有效性",)
        return FreshnessAssessment(
            symbol=assessment.symbol,
            decision=decision,
            checked_at=now,
            original_price=original,
            latest_price=latest,
            drift_percent=drift_percent,
            drift_atr_1m=None,
            invalidation_buffer_consumed=None,
            spread_bps=ticker.spread_bps,
            depth_retention=None,
            book_available=False,
            reasons=reasons,
        )


def _invalidation_checks(
    assessment: CandidateAssessment,
) -> tuple[_InvalidationCheck, ...]:
    invalidation = assessment.invalidation
    direction = assessment.direction
    if invalidation is None or direction is None:
        return ()
    condition = invalidation.condition.lower().replace(" ", "")
    timeframes = []
    for suffix, timeframe in _INVALIDATION_SUFFIXES.items():
        cited = any(evidence_id.endswith(suffix) for evidence_id in invalidation.evidence_ids)
        named = timeframe in condition or timeframe.replace("m", "分钟") in condition
        if cited and named:
            timeframes.append(timeframe)
    if not timeframes:
        timeframes = [
            timeframe
            for suffix, timeframe in _INVALIDATION_SUFFIXES.items()
            if any(evidence_id.endswith(suffix) for evidence_id in invalidation.evidence_ids)
        ]
    comparator = (
        Comparator.LESS_THAN
        if direction is Direction.LONG_BIAS
        else Comparator.GREATER_THAN
    )
    return tuple(
        _InvalidationCheck(
            timeframe=timeframe,
            comparator=comparator,
            threshold=invalidation.reference_price,
        )
        for timeframe in timeframes[:1]
    )
