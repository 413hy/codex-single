from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import Comparator, MonitoringMetric, ThresholdSeverity
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    MonitoringDirective,
    SignalConclusion,
)

_ACTIVE_MONITORING_METRICS = frozenset(
    {
        MonitoringMetric.LAST_PRICE,
        MonitoringMetric.MARK_PRICE,
        MonitoringMetric.COMPLETED_1M_CLOSE,
        MonitoringMetric.COMPLETED_5M_CLOSE,
        MonitoringMetric.COMPLETED_15M_CLOSE,
        MonitoringMetric.COMPLETED_30M_CLOSE,
        MonitoringMetric.COMPLETED_1H_CLOSE,
        MonitoringMetric.TURNOVER_1M,
        MonitoringMetric.TRADE_DELTA_30S,
        MonitoringMetric.SPREAD_BPS,
        MonitoringMetric.ORDERBOOK_IMBALANCE_L5,
        MonitoringMetric.OPEN_INTEREST,
        MonitoringMetric.FUNDING_RATE,
        MonitoringMetric.LIQUIDATION_NOTIONAL_1M,
    }
)


@dataclass(frozen=True, slots=True)
class ActiveDirective:
    analysis_id: str
    symbol: str
    created_at: datetime
    directive: MonitoringDirective

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.directive.valid_for_seconds)


@dataclass(frozen=True, slots=True)
class TriggeredCondition:
    metric: MonitoringMetric
    comparator: Comparator
    threshold: Decimal
    observed_value: Decimal


@dataclass(frozen=True, slots=True)
class ThresholdEvent:
    analysis_id: str
    symbol: str
    family_id: str
    metric: MonitoringMetric
    observed_value: Decimal
    threshold: Decimal
    observed_at: datetime
    reason: str
    critical: bool
    matched_conditions: tuple[TriggeredCondition, ...] = ()

    @property
    def event_id(self) -> str:
        stamp = int(self.observed_at.timestamp() * 1000)
        return f"{self.analysis_id}:{self.symbol}:{self.family_id}:{stamp}"


@dataclass(slots=True)
class _DirectiveState:
    active: ActiveDirective
    armed: bool | None = None
    condition_since: datetime | None = None
    suspended: bool = False
    values: dict[MonitoringMetric, Decimal] | None = None
    consecutive_primary_hits: int = 0
    last_primary_observed_at: datetime | None = None


class ThresholdEngine:
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _DirectiveState] = {}

    def replace_cycle(
        self,
        cycle: AnalysisCycleResult | None,
        *,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        self.replace_conclusions(
            cycle.conclusions if cycle is not None else (),
            now=current_time,
        )

    def replace_conclusions(
        self,
        conclusions: tuple[SignalConclusion, ...],
        *,
        now: datetime | None = None,
        suspended_keys: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        current_time = now or datetime.now(UTC)
        replacement: dict[tuple[str, str], _DirectiveState] = {}
        for conclusion in conclusions:
            for directive in conclusion.assessment.monitoring_directives:
                # Target-reaching thresholds were retired. Other qualified market metrics
                # remain review triggers: crossing wakes Codex, but does not itself label
                # the direction invalid.
                if (
                    directive.metric not in _ACTIVE_MONITORING_METRICS
                    or _is_target_directive(directive)
                ):
                    continue
                active = ActiveDirective(
                    analysis_id=conclusion.analysis_id,
                    symbol=conclusion.assessment.symbol,
                    created_at=conclusion.generated_at,
                    directive=directive,
                )
                if active.expires_at <= current_time:
                    continue
                key = (active.symbol, directive.family_id)
                version_key = (active.analysis_id, active.symbol)
                previous = self._states.get(key)
                if previous is not None and previous.active == active:
                    if version_key in suspended_keys:
                        previous.suspended = True
                    replacement[key] = previous
                else:
                    values = {
                        directive.metric: directive.current_value
                    } if directive.current_value is not None else {}
                    values.update(
                        {
                            confirmation.metric: confirmation.current_value
                            for confirmation in directive.confirmations
                        }
                    )
                    complete = self._group_condition(values, directive)
                    replacement[key] = _DirectiveState(
                        active=active,
                        armed=(
                            not complete
                            if self._has_all_values(values, directive)
                            else None
                        ),
                        suspended=version_key in suspended_keys,
                        values=values,
                    )
        self._states = replacement

    def directives(self, *, include_suspended: bool = False) -> tuple[ActiveDirective, ...]:
        return tuple(
            state.active
            for state in self._states.values()
            if include_suspended or not state.suspended
        )

    def observe(
        self,
        *,
        symbol: str,
        metric: MonitoringMetric,
        value: Decimal,
        observed_at: datetime,
        confirmation_seconds: int = 0,
    ) -> tuple[ThresholdEvent, ...]:
        for key, state in tuple(self._states.items()):
            active = state.active
            directive = active.directive
            if active.expires_at <= observed_at:
                del self._states[key]
                continue
            observed_metrics = {
                directive.metric,
                *(confirmation.metric for confirmation in directive.confirmations),
            }
            if state.suspended or active.symbol != symbol or metric not in observed_metrics:
                continue
            if observed_at <= active.created_at:
                continue
            if state.values is None:
                state.values = {}
            state.values[metric] = value
            if metric is directive.metric:
                primary_met = self._condition(value, directive)
                if not primary_met:
                    state.consecutive_primary_hits = 0
                    state.last_primary_observed_at = observed_at
                elif self._is_new_primary_observation(state, observed_at, directive.metric):
                    state.consecutive_primary_hits += 1
                    state.last_primary_observed_at = observed_at
            if not self._has_all_values(state.values, directive):
                continue
            condition = self._group_condition(state.values, directive)
            if state.armed is None:
                state.armed = not condition
                continue
            if (
                state.armed
                and condition
                and state.consecutive_primary_hits
                >= directive.required_consecutive_observations
            ):
                required_confirmation = max(
                    confirmation_seconds, directive.confirmation_seconds
                )
                if required_confirmation > 0:
                    if state.condition_since is None:
                        state.condition_since = observed_at
                        continue
                    if (
                        observed_at - state.condition_since
                    ).total_seconds() < required_confirmation:
                        continue
                state.condition_since = None
                event = self._event(active, state.values, observed_at)
                self._suspend_version(active.analysis_id, active.symbol)
                return (event,)
            elif state.armed:
                state.condition_since = None
            elif not state.armed and self._group_rearmed(state.values, directive):
                state.armed = True
                state.condition_since = None
        return ()

    @staticmethod
    def _is_new_primary_observation(
        state: _DirectiveState,
        observed_at: datetime,
        metric: MonitoringMetric,
    ) -> bool:
        previous = state.last_primary_observed_at
        if previous is None:
            return True
        minimum_spacing = {
            MonitoringMetric.COMPLETED_1M_CLOSE: 45,
            MonitoringMetric.COMPLETED_5M_CLOSE: 240,
            MonitoringMetric.COMPLETED_15M_CLOSE: 720,
            MonitoringMetric.COMPLETED_30M_CLOSE: 1_440,
            MonitoringMetric.COMPLETED_1H_CLOSE: 2_880,
        }.get(metric, 0)
        return (observed_at - previous).total_seconds() >= minimum_spacing

    def _suspend_version(self, analysis_id: str, symbol: str) -> None:
        for state in self._states.values():
            active = state.active
            if active.analysis_id == analysis_id and active.symbol == symbol:
                state.suspended = True
                state.condition_since = None

    @staticmethod
    def _event(
        active: ActiveDirective,
        values: dict[MonitoringMetric, Decimal],
        observed_at: datetime,
    ) -> ThresholdEvent:
        directive = active.directive
        matched_conditions = (
            TriggeredCondition(
                metric=directive.metric,
                comparator=directive.comparator,
                threshold=directive.threshold,
                observed_value=values[directive.metric],
            ),
            *(
                TriggeredCondition(
                    metric=confirmation.metric,
                    comparator=confirmation.comparator,
                    threshold=confirmation.threshold,
                    observed_value=values[confirmation.metric],
                )
                for confirmation in directive.confirmations
            ),
        )
        return ThresholdEvent(
            analysis_id=active.analysis_id,
            symbol=active.symbol,
            family_id=directive.family_id,
            metric=directive.metric,
            observed_value=values[directive.metric],
            threshold=directive.threshold,
            observed_at=observed_at,
            reason=directive.reason,
            critical=(
                directive.severity is ThresholdSeverity.CRITICAL
                or "invalidation" in directive.family_id
            ),
            matched_conditions=matched_conditions,
        )

    @staticmethod
    def _condition(value: Decimal, directive: MonitoringDirective) -> bool:
        if directive.comparator is Comparator.GREATER_THAN:
            return value > directive.threshold
        return value < directive.threshold

    @staticmethod
    def _has_all_values(
        values: dict[MonitoringMetric, Decimal],
        directive: MonitoringDirective,
    ) -> bool:
        return directive.metric in values and all(
            confirmation.metric in values for confirmation in directive.confirmations
        )

    @staticmethod
    def _group_condition(
        values: dict[MonitoringMetric, Decimal],
        directive: MonitoringDirective,
    ) -> bool:
        primary = values.get(directive.metric)
        if primary is None or not ThresholdEngine._condition(primary, directive):
            return False
        return all(
            (
                values[confirmation.metric] > confirmation.threshold
                if confirmation.comparator is Comparator.GREATER_THAN
                else values[confirmation.metric] < confirmation.threshold
            )
            for confirmation in directive.confirmations
        )

    @staticmethod
    def _group_rearmed(
        values: dict[MonitoringMetric, Decimal],
        directive: MonitoringDirective,
    ) -> bool:
        primary = values.get(directive.metric)
        if primary is None:
            return False
        return ThresholdEngine._rearmed(primary, directive)

    @staticmethod
    def _rearmed(value: Decimal, directive: MonitoringDirective) -> bool:
        if directive.comparator is Comparator.GREATER_THAN:
            return value <= directive.threshold - directive.hysteresis
        return value >= directive.threshold + directive.hysteresis


def _is_target_directive(directive: MonitoringDirective) -> bool:
    family_parts = directive.family_id.replace(":", ".").split(".")
    if any(part in {"target", "tp", "takeprofit", "take_profit"} for part in family_parts):
        return True
    reason = directive.reason.lower()
    return "take-profit" in reason or "take profit" in reason or any(
        marker in directive.reason for marker in ("止盈", "目标已", "目标价", "目标结构")
    )


class WakeLimiter:
    def __init__(self, config: MonitoringConfig) -> None:
        self._config = config
        self._hourly_wakes: deque[datetime] = deque()

    def allow(self, *, now: datetime, critical: bool) -> bool:
        """Record a wake and report whether it is inside the advisory rate budget.

        The result is advisory and must not discard a confirmed crossing. Whole-set
        one-shot suspension prevents duplicate wakes within an analysis version.
        """

        if critical:
            return True
        cutoff = now - timedelta(hours=1)
        while self._hourly_wakes and self._hourly_wakes[0] <= cutoff:
            self._hourly_wakes.popleft()
        within_budget = len(self._hourly_wakes) < self._config.soft_model_wakes_per_hour
        self._hourly_wakes.append(now)
        return within_budget
