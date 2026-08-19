from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.domain.enums import Direction, MonitoringMetric
from bybit_signal.domain.models import Candle, CandleOutlook, SignalConclusion, SignalOutcome
from bybit_signal.providers.bybit import BybitPublicClient
from bybit_signal.storage.sqlite import SignalStore


class OutcomeEvaluator:
    """Settles predictions and market structure outcomes without modifying strategy."""

    def __init__(self, client: BybitPublicClient, store: SignalStore) -> None:
        self._client = client
        self._store = store

    async def settle_ready(self) -> tuple[SignalOutcome, ...]:
        now = datetime.now(UTC)
        pending: list[SignalConclusion] = []
        for cycle in await self._store.recent_scheduled_cycles(limit=30):
            for conclusion in cycle.conclusions:
                assessment = conclusion.assessment
                if assessment.selection_rank is None or assessment.direction is None:
                    continue
                windows = [
                    value
                    for value in (
                        assessment.forming_15m,
                        assessment.forming_30m,
                        assessment.forming_1h,
                        assessment.next_15m,
                    )
                    if value is not None
                ]
                if not windows or max(value.window_end for value in windows) > now:
                    continue
                if await self._store.outcome_exists(conclusion.analysis_id, assessment.symbol):
                    continue
                pending.append(conclusion)

        async def evaluate(conclusion: SignalConclusion) -> SignalOutcome | None:
            metric = _formal_invalidation_metric(conclusion)
            timeframe = _INVALIDATION_TIMEFRAMES.get(metric) if metric is not None else None
            tasks = [
                self._client.completed_candles(
                    conclusion.assessment.symbol,
                    timeframe="1m",
                    limit=240,
                )
            ]
            if timeframe is not None:
                tasks.append(
                    self._client.completed_candles(
                        conclusion.assessment.symbol,
                        timeframe=timeframe,  # type: ignore[arg-type]
                        limit=240,
                    )
                )
            results = await asyncio.gather(*tasks)
            return _evaluate(
                conclusion,
                results[0],
                now,
                invalidation_candles=(results[1] if len(results) > 1 else ()),
            )

        values = await asyncio.gather(
            *(evaluate(conclusion) for conclusion in pending), return_exceptions=True
        )
        outcomes = tuple(value for value in values if isinstance(value, SignalOutcome))
        await self._store.save_outcomes(outcomes)
        return outcomes


def _evaluate(
    conclusion: SignalConclusion,
    candles: Sequence[Candle],
    evaluated_at: datetime,
    *,
    invalidation_candles: Sequence[Candle] = (),
) -> SignalOutcome | None:
    assessment = conclusion.assessment
    direction = assessment.direction
    if direction is None:
        return None
    first_full_minute = conclusion.generated_at.replace(second=0, microsecond=0)
    if first_full_minute < conclusion.generated_at:
        first_full_minute += timedelta(minutes=1)
    relevant = [
        candle
        for candle in candles
        if candle.open_time >= first_full_minute and candle.close_time <= evaluated_at
    ]
    if not relevant or relevant[0].open_time != first_full_minute:
        return None
    reference = conclusion.canonical_price.value
    highs = [candle.high for candle in relevant]
    lows = [candle.low for candle in relevant]
    if direction is Direction.LONG_BIAS:
        mfe = (max(highs) / reference - 1) * 100
        mae = (min(lows) / reference - 1) * 100
    else:
        mfe = (reference / min(lows) - 1) * 100
        mae = (reference / max(highs) - 1) * 100
    formal_metric = _formal_invalidation_metric(conclusion)
    invalidation = assessment.invalidation
    invalidation_touch_at = (
        next(
            (
                candle.close_time
                for candle in invalidation_candles
                if candle.close_time > conclusion.generated_at
                and candle.close_time <= evaluated_at
                and invalidation is not None
                and (
                    candle.close > invalidation.reference_price
                    if direction is Direction.SHORT_BIAS
                    else candle.close < invalidation.reference_price
                )
            ),
            None,
        )
        if formal_metric is not None and invalidation is not None
        else None
    )
    invalidation_touched = invalidation_touch_at is not None
    forecasts = {
        "forming_15m_correct": _outlook_correct(assessment.forming_15m, candles),
        "forming_30m_correct": _outlook_correct(assessment.forming_30m, candles),
        "forming_1h_correct": _outlook_correct(assessment.forming_1h, candles),
        "next_15m_correct": _outlook_correct(assessment.next_15m, candles),
    }
    error_type = None
    if forecasts["next_15m_correct"] is False:
        error_type = "NEXT_15M_DIRECTION_WRONG"
    elif invalidation_touch_at is not None:
        error_type = "DIRECTION_STRUCTURE_INVALIDATED"
    return SignalOutcome(
        analysis_id=conclusion.analysis_id,
        symbol=assessment.symbol,
        evaluated_at=evaluated_at,
        signal_time=conclusion.generated_at,
        direction=direction,
        reference_price=reference,
        latest_price=relevant[-1].close,
        # Kept nullable only for reading historical outcome rows. Current evaluation
        # never grades the display-only approximate take-profit.
        target_touched=None,
        invalidation_touched=invalidation_touched,
        mfe_percent=_finite_decimal(mfe),
        mae_percent=_finite_decimal(mae),
        error_type=error_type,
        details={
            "candle_count": len(relevant),
            "first_candle_open": relevant[0].open_time.isoformat(),
            "last_candle_close": relevant[-1].close_time.isoformat(),
            "invalidation_touch_at": (
                invalidation_touch_at.isoformat()
                if invalidation_touch_at is not None
                else None
            ),
            "invalidation_metric": formal_metric.value if formal_metric is not None else None,
        },
        **forecasts,
    )


def _outlook_correct(
    outlook: CandleOutlook | None,
    candles: Sequence[Candle],
) -> bool | None:
    if outlook is None:
        return None
    values = [
        candle
        for candle in candles
        if candle.open_time >= outlook.window_start and candle.close_time <= outlook.window_end
    ]
    if not values or values[0].open_time != outlook.window_start:
        return None
    change = values[-1].close - values[0].open
    if change == 0:
        return False
    return change > 0 if outlook.direction is Direction.LONG_BIAS else change < 0


def _finite_decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


_INVALIDATION_TIMEFRAMES: dict[MonitoringMetric, str] = {
    MonitoringMetric.COMPLETED_5M_CLOSE: "5m",
    MonitoringMetric.COMPLETED_15M_CLOSE: "15m",
    MonitoringMetric.COMPLETED_30M_CLOSE: "30m",
    MonitoringMetric.COMPLETED_1H_CLOSE: "1h",
}


def _formal_invalidation_metric(
    conclusion: SignalConclusion,
) -> MonitoringMetric | None:
    invalidation = conclusion.assessment.invalidation
    if invalidation is None:
        return None
    condition = invalidation.condition.lower().replace(" ", "")
    candidates = (
        (".PA.5M", "5m", MonitoringMetric.COMPLETED_5M_CLOSE),
        (".PA.15M", "15m", MonitoringMetric.COMPLETED_15M_CLOSE),
        (".PA.30M", "30m", MonitoringMetric.COMPLETED_30M_CLOSE),
        (".PA.1H", "1h", MonitoringMetric.COMPLETED_1H_CLOSE),
    )
    cited = [
        (timeframe, metric)
        for suffix, timeframe, metric in candidates
        if any(evidence_id.endswith(suffix) for evidence_id in invalidation.evidence_ids)
    ]
    for timeframe, metric in cited:
        if timeframe in condition or timeframe.replace("m", "分钟") in condition:
            return metric
    return cited[0][1] if cited else None
