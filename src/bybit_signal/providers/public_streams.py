from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bybit_signal.domain.enums import ToolStatus
from bybit_signal.domain.models import Symbol


class PublicLiquidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    timestamp: datetime
    liquidated_position: Literal["LONG", "SHORT"]
    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class LiquidationWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    generated_at: datetime
    window_seconds: int = Field(gt=0)
    status: ToolStatus
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    coverage_complete: bool = False
    event_count: int | None = Field(default=None, ge=0)
    long_notional: Decimal | None = Field(default=None, ge=0)
    short_notional: Decimal | None = Field(default=None, ge=0)
    largest_notional: Decimal | None = Field(default=None, ge=0)
    reconnect_count: int = Field(default=0, ge=0)


class PublicStreamCache:
    """Bounded public-stream facts; uncovered sparse windows remain unknown."""

    def __init__(self, *, retention_seconds: int = 7200) -> None:
        self._retention = timedelta(seconds=retention_seconds)
        self._events: dict[str, deque[PublicLiquidation]] = {}
        self._coverage_start: dict[str, datetime] = {}
        self._heartbeat: dict[str, datetime] = {}
        self._reconnects: dict[str, int] = {}

    def mark_subscribed(self, symbol: str, *, at: datetime | None = None) -> None:
        now = at or datetime.now(UTC)
        self._coverage_start.setdefault(symbol, now)
        self._heartbeat[symbol] = now

    def heartbeat(self, symbol: str, *, at: datetime | None = None) -> None:
        self._heartbeat[symbol] = at or datetime.now(UTC)

    def mark_reconnect(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            self._reconnects[symbol] = self._reconnects.get(symbol, 0) + 1
            self._coverage_start.pop(symbol, None)

    def record_liquidation(self, event: PublicLiquidation) -> None:
        values = self._events.setdefault(event.symbol, deque())
        values.append(event)
        self.heartbeat(event.symbol, at=event.timestamp)
        self._prune(event.timestamp)

    def liquidation_window(
        self,
        symbol: str,
        *,
        seconds: int = 300,
        now: datetime | None = None,
    ) -> LiquidationWindow:
        current = now or datetime.now(UTC)
        self._prune(current)
        cutoff = current - timedelta(seconds=seconds)
        coverage_start = self._coverage_start.get(symbol)
        coverage_end = self._heartbeat.get(symbol)
        complete = bool(
            coverage_start is not None
            and coverage_start <= cutoff
            and coverage_end is not None
            and coverage_end >= current - timedelta(seconds=60)
        )
        events = tuple(
            event for event in self._events.get(symbol, ()) if cutoff < event.timestamp <= current
        )
        available = complete or bool(events)
        status = (
            ToolStatus.AVAILABLE
            if complete
            else ToolStatus.PARTIAL
            if events
            else ToolStatus.WARMING_UP
            if coverage_start is not None
            else ToolStatus.UNAVAILABLE
        )
        return LiquidationWindow(
            symbol=symbol,
            generated_at=current,
            window_seconds=seconds,
            status=status,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            coverage_complete=complete,
            event_count=len(events) if available else None,
            long_notional=(
                sum(
                    (event.notional for event in events if event.liquidated_position == "LONG"),
                    Decimal(0),
                )
                if available
                else None
            ),
            short_notional=(
                sum(
                    (event.notional for event in events if event.liquidated_position == "SHORT"),
                    Decimal(0),
                )
                if available
                else None
            ),
            largest_notional=max((event.notional for event in events), default=Decimal(0))
            if available
            else None,
            reconnect_count=self._reconnects.get(symbol, 0),
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._retention
        for symbol, values in tuple(self._events.items()):
            while values and values[0].timestamp <= cutoff:
                values.popleft()
            if not values:
                self._events.pop(symbol, None)
