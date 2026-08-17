from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bybit_signal.config import MonitoringConfig
from bybit_signal.domain.enums import Comparator, MonitoringMetric
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    MonitoringDirective,
    SignalConclusion,
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


@dataclass(slots=True)
class _DirectiveState:
    active: ActiveDirective
    armed: bool | None = None


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
    ) -> None:
        current_time = now or datetime.now(UTC)
        replacement: dict[tuple[str, str], _DirectiveState] = {}
        for conclusion in conclusions:
            for directive in conclusion.assessment.monitoring_directives:
                active = ActiveDirective(
                    analysis_id=conclusion.analysis_id,
                    symbol=conclusion.assessment.symbol,
                    created_at=conclusion.generated_at,
                    directive=directive,
                )
                if active.expires_at <= current_time:
                    continue
                key = (active.symbol, directive.family_id)
                previous = self._states.get(key)
                if previous is not None and previous.active == active:
                    replacement[key] = previous
                else:
                    replacement[key] = _DirectiveState(active=active)
        self._states = replacement

    def directives(self) -> tuple[ActiveDirective, ...]:
        return tuple(state.active for state in self._states.values())

    def observe(
        self,
        *,
        symbol: str,
        metric: MonitoringMetric,
        value: Decimal,
        observed_at: datetime,
    ) -> tuple[ThresholdEvent, ...]:
        events: list[ThresholdEvent] = []
        for key, state in tuple(self._states.items()):
            active = state.active
            directive = active.directive
            if active.expires_at <= observed_at:
                del self._states[key]
                continue
            if active.symbol != symbol or directive.metric is not metric:
                continue
            condition = self._condition(value, directive)
            if state.armed is None:
                state.armed = not condition
                continue
            if state.armed and condition:
                state.armed = False
                events.append(
                    ThresholdEvent(
                        analysis_id=active.analysis_id,
                        symbol=symbol,
                        family_id=directive.family_id,
                        metric=metric,
                        observed_value=value,
                        threshold=directive.threshold,
                        observed_at=observed_at,
                        reason=directive.reason,
                        critical="invalidation" in directive.family_id,
                    )
                )
            elif not state.armed and self._rearmed(value, directive):
                state.armed = True
        return tuple(events)

    @staticmethod
    def _condition(value: Decimal, directive: MonitoringDirective) -> bool:
        if directive.comparator is Comparator.GREATER_THAN:
            return value > directive.threshold
        return value < directive.threshold

    @staticmethod
    def _rearmed(value: Decimal, directive: MonitoringDirective) -> bool:
        if directive.comparator is Comparator.GREATER_THAN:
            return value <= directive.threshold - directive.hysteresis
        return value >= directive.threshold + directive.hysteresis


class WakeLimiter:
    def __init__(self, config: MonitoringConfig) -> None:
        self._config = config
        self._last_symbol_wake: dict[str, datetime] = {}
        self._hourly_wakes: deque[datetime] = deque()

    def allow(self, symbol: str, *, now: datetime, critical: bool) -> bool:
        previous = self._last_symbol_wake.get(symbol)
        if (
            previous is not None
            and (now - previous).total_seconds() < self._config.symbol_cooldown_seconds
        ):
            return False
        cutoff = now - timedelta(hours=1)
        while self._hourly_wakes and self._hourly_wakes[0] <= cutoff:
            self._hourly_wakes.popleft()
        if not critical and len(self._hourly_wakes) >= self._config.soft_model_wakes_per_hour:
            return False
        self._last_symbol_wake[symbol] = now
        self._hourly_wakes.append(now)
        return True
