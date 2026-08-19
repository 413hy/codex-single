from __future__ import annotations

import asyncio
import itertools
import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from bybit_signal.config import ScannerConfig
from bybit_signal.domain.enums import CandidateRiskTag
from bybit_signal.domain.models import Candle, Symbol
from bybit_signal.providers.bybit import (
    BybitInstrument,
    BybitOrderBook,
    BybitPublicClient,
    BybitTicker,
)


class CandidateFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    median_range_percent: float = Field(ge=0)
    realized_volatility_percent: float = Field(ge=0)
    maximum_absolute_return_percent: float = Field(ge=0)
    direction_efficiency: float = Field(ge=0, le=1)
    signed_return_30m_percent: float = 0
    overlap_ratio: float = Field(default=0, ge=0, le=1)
    atr_activity_ratio: float = Field(default=0, ge=0)
    impact_extension_atr: float = Field(default=0, ge=0)
    recent_turnover_ratio: float = Field(ge=0)
    recent_30m_turnover_usdt: float = Field(ge=0)
    spread_bps: float = Field(ge=0)
    depth_top20_usdt: float | None = Field(default=None, ge=0)
    turnover_24h_usdt: float = Field(ge=0)
    completed_candles: int = Field(ge=0)
    missing_intervals: int = Field(ge=0)


class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1)
    symbol: Symbol
    score: float = Field(ge=0, le=100)
    opportunity_score: float = Field(default=0, ge=0, le=100)
    tradability_score: float = Field(default=0, ge=0, le=100)
    last_price: Decimal = Field(gt=0)
    observed_at: datetime
    features: CandidateFeatures
    risk_tags: tuple[CandidateRiskTag, ...] = ()
    reasons: tuple[str, ...] = Field(min_length=1)


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    universe_size: int = Field(ge=0)
    ticker_eligible_size: int = Field(ge=0)
    kline_analyzed_size: int = Field(ge=0)
    candidates: tuple[RankedCandidate, ...]
    failures: dict[str, str]


class _UnrankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    opportunity_raw: float = Field(ge=0)
    tradability_score: float = Field(ge=0, le=100)
    ticker: BybitTicker
    features: CandidateFeatures
    risk_tags: tuple[CandidateRiskTag, ...]
    reasons: tuple[str, ...]


class BybitUniverseScanner:
    """Two-dimensional filter: opportunity first, tradability as a safety constraint."""

    def __init__(self, client: BybitPublicClient, config: ScannerConfig) -> None:
        self._client = client
        self._config = config

    async def scan(self, *, limit: int = 5) -> ScanResult:
        instruments, tickers = await asyncio.gather(
            self._client.instruments(), self._client.tickers()
        )
        instrument_map = {instrument.symbol: instrument for instrument in instruments}
        eligible = [
            (instrument_map[symbol], ticker)
            for symbol, ticker in tickers.items()
            if symbol in instrument_map and self._ticker_eligible(ticker)
        ]
        eligible.sort(key=lambda item: self._preselection_score(item[1]), reverse=True)
        selected = eligible[: self._config.preselect_limit]
        semaphore = asyncio.Semaphore(self._config.request_concurrency)
        failures: dict[str, str] = {}

        async def analyze(
            instrument: BybitInstrument, ticker: BybitTicker
        ) -> _UnrankedCandidate | None:
            async with semaphore:
                try:
                    candles = await self._client.completed_5m_candles(
                        instrument.symbol, limit=self._config.candle_limit
                    )
                except Exception as error:
                    failures[instrument.symbol] = type(error).__name__
                    return None
                book: BybitOrderBook | None = None
                try:
                    book = await self._client.orderbook(instrument.symbol, limit=50)
                except Exception as error:
                    failures[f"{instrument.symbol}.orderbook"] = type(error).__name__
                return self._candidate(ticker, candles, book)

        analyzed = await asyncio.gather(
            *(analyze(instrument, ticker) for instrument, ticker in selected)
        )
        unranked = [candidate for candidate in analyzed if candidate is not None]
        ranked = _rank_candidates(unranked, limit, self._config.minimum_tradability_score)
        return ScanResult(
            generated_at=datetime.now(UTC),
            universe_size=len(instruments),
            ticker_eligible_size=len(eligible),
            kline_analyzed_size=sum(value is not None for value in analyzed),
            candidates=ranked,
            failures=failures,
        )

    def _ticker_eligible(self, ticker: BybitTicker) -> bool:
        return (
            float(ticker.turnover_24h) >= self._config.minimum_24h_turnover_usdt
            and float(ticker.spread_bps) <= self._config.maximum_spread_bps
            and ticker.ask_price >= ticker.bid_price
        )

    def _preselection_score(self, ticker: BybitTicker) -> float:
        activity = min(
            1.0,
            math.log10(max(float(ticker.turnover_24h), 1.0)) / math.log10(1_000_000_000),
        )
        movement = max(
            float(ticker.range_24h_percent) / 100,
            abs(float(ticker.price_change_24h)),
        )
        spread_penalty = min(1.0, float(ticker.spread_bps) / self._config.maximum_spread_bps)
        return movement * 0.72 + activity * 0.23 - spread_penalty * 0.05

    def _candidate(
        self,
        ticker: BybitTicker,
        candles: tuple[Candle, ...],
        book: BybitOrderBook | None,
    ) -> _UnrankedCandidate | None:
        if len(candles) < self._config.minimum_completed_candles:
            return None
        candles = candles[-self._config.candle_limit :]
        missing_intervals = _missing_intervals(candles)
        if missing_intervals > self._config.maximum_missing_intervals:
            return None
        closes = [float(candle.close) for candle in candles]
        highs = [float(candle.high) for candle in candles]
        lows = [float(candle.low) for candle in candles]
        ranges = [
            (high - low) / close * 100 for high, low, close in zip(highs, lows, closes, strict=True)
        ]
        returns = [(closes[index] / closes[index - 1] - 1) * 100 for index in range(1, len(closes))]
        median_range = statistics.median(ranges)
        maximum_return = max(abs(value) for value in returns)
        if (
            median_range < self._config.minimum_median_range_percent
            and maximum_return < self._config.minimum_max_return_percent
        ):
            return None
        realized = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        total_path = sum(abs(value) for value in returns[-12:])
        signed_return_30m = (closes[-1] / closes[-7] - 1) * 100
        efficiency = abs(signed_return_30m) / total_path if total_path else 0.0
        overlap = _overlap_ratio(highs[-12:], lows[-12:])
        recent_turnover = sum(float(candle.turnover) for candle in candles[-6:])
        previous_turnover = sum(float(candle.turnover) for candle in candles[-12:-6])
        turnover_ratio = recent_turnover / previous_turnover if previous_turnover > 0 else 0.0
        if recent_turnover < self._config.minimum_recent_30m_turnover_usdt:
            return None
        atr = statistics.fmean(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
            for index in range(max(1, len(closes) - 14), len(closes))
        )
        latest_range = highs[-1] - lows[-1]
        atr_activity = latest_range / atr if atr > 0 else 0.0
        rolling_mid = (max(highs[-20:]) + min(lows[-20:])) / 2
        extension = abs(closes[-1] - rolling_mid) / atr if atr > 0 else 0.0
        depth = None
        if book is not None:
            bid = sum(float(level.notional) for level in book.bids[:20])
            ask = sum(float(level.notional) for level in book.asks[:20])
            depth = min(bid, ask)
        features = CandidateFeatures(
            median_range_percent=median_range,
            realized_volatility_percent=realized,
            maximum_absolute_return_percent=maximum_return,
            direction_efficiency=min(1.0, efficiency),
            signed_return_30m_percent=signed_return_30m,
            overlap_ratio=overlap,
            atr_activity_ratio=max(0.0, atr_activity),
            impact_extension_atr=max(0.0, extension),
            recent_turnover_ratio=max(0.0, turnover_ratio),
            recent_30m_turnover_usdt=max(0.0, recent_turnover),
            spread_bps=float(ticker.spread_bps),
            depth_top20_usdt=depth,
            turnover_24h_usdt=float(ticker.turnover_24h),
            completed_candles=len(candles),
            missing_intervals=missing_intervals,
        )
        opportunity = (
            median_range * 0.22
            + realized * 0.23
            + maximum_return * 0.18
            + min(3.0, turnover_ratio) * 0.10
            + abs(signed_return_30m) * 0.10
            + min(3.0, atr_activity) * 0.08
            + (1 - overlap) * 0.05
            + min(1.0, efficiency) * 0.04
        )
        tradability = self._tradability_score(ticker, recent_turnover, depth, missing_intervals)
        tags: list[CandidateRiskTag] = []
        if recent_turnover < self._config.minimum_recent_30m_turnover_usdt * 2 or (
            depth is not None and depth < self._config.thin_depth_notional_usdt
        ):
            tags.append(CandidateRiskTag.THIN_LIQUIDITY)
        if overlap >= self._config.choppy_overlap_ratio and efficiency < 0.25:
            tags.append(CandidateRiskTag.CHOPPY)
        if extension >= self._config.impact_extension_atr:
            tags.append(CandidateRiskTag.IMPACT_EXTENDED)
        if book is None or missing_intervals > 0:
            tags.append(CandidateRiskTag.DATA_DEGRADED)
        reasons = (
            f"机会: 5m中位振幅 {median_range:.3f}%, 实现波动 {realized:.3f}%",
            f"机会: 近30m变化 {signed_return_30m:.3f}%, 方向效率 {efficiency:.2f}",
            f"成交: 近30m {recent_turnover:,.0f} USDT / 前窗 {turnover_ratio:.2f}x",
            "可交易性: 24h "
            f"{float(ticker.turnover_24h):,.0f} USDT, "
            f"点差 {float(ticker.spread_bps):.2f} bps",
            f"盘口: top20单侧较小深度 {depth:,.0f} USDT"
            if depth is not None
            else "盘口: 本轮快照不可用",
        )
        return _UnrankedCandidate(
            symbol=ticker.symbol,
            opportunity_raw=max(0.0, opportunity),
            tradability_score=tradability,
            ticker=ticker,
            features=features,
            risk_tags=tuple(tags),
            reasons=reasons,
        )

    def _tradability_score(
        self,
        ticker: BybitTicker,
        recent_turnover: float,
        depth: float | None,
        missing_intervals: int,
    ) -> float:
        turnover_24h = min(1.0, math.log10(max(float(ticker.turnover_24h), 1)) / 9)
        recent = min(1.0, math.log10(max(recent_turnover, 1)) / 7)
        spread = max(0.0, 1 - float(ticker.spread_bps) / self._config.maximum_spread_bps)
        depth_score = min(1.0, math.log10(max(depth, 1)) / 6) if depth is not None else 0.30
        continuity = max(
            0.0, 1 - missing_intervals / max(1, self._config.maximum_missing_intervals + 1)
        )
        return round(
            max(
                0.0,
                min(
                    1.0,
                    turnover_24h * 0.25
                    + recent * 0.25
                    + spread * 0.22
                    + depth_score * 0.20
                    + continuity * 0.08,
                ),
            )
            * 100,
            3,
        )


def _missing_intervals(candles: tuple[Candle, ...]) -> int:
    expected = timedelta(minutes=5)
    missing = 0
    for previous, current in itertools.pairwise(candles):
        gap = current.open_time - previous.open_time
        if gap > expected:
            missing += max(0, round(gap / expected) - 1)
    return missing


def _overlap_ratio(highs: list[float], lows: list[float]) -> float:
    if len(highs) < 2:
        return 0.0
    overlap = 0.0
    span = 0.0
    for previous_high, previous_low, high, low in zip(
        highs, lows, highs[1:], lows[1:], strict=False
    ):
        overlap += max(0.0, min(previous_high, high) - max(previous_low, low))
        span += max(previous_high, high) - min(previous_low, low)
    return min(1.0, overlap / span) if span > 0 else 0.0


def _rank_candidates(
    candidates: list[_UnrankedCandidate],
    limit: int,
    minimum_tradability: float,
) -> tuple[RankedCandidate, ...]:
    if not candidates or limit <= 0:
        return ()
    raw = [candidate.opportunity_raw for candidate in candidates]
    high, low = max(raw), min(raw)
    span = high - low
    scored: list[tuple[float, float, _UnrankedCandidate]] = []
    for candidate in candidates:
        opportunity = 100.0 if span == 0 else (candidate.opportunity_raw - low) / span * 100
        final = opportunity * 0.72 + candidate.tradability_score * 0.28
        if candidate.tradability_score >= minimum_tradability:
            scored.append((final, opportunity, candidate))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    maximum_final = scored[0][0] if scored else 0
    return tuple(
        RankedCandidate(
            rank=index,
            symbol=candidate.symbol,
            score=round(final / maximum_final * 100, 3) if maximum_final else 0,
            opportunity_score=round(opportunity, 3),
            tradability_score=candidate.tradability_score,
            last_price=candidate.ticker.last_price,
            observed_at=candidate.ticker.observed_at,
            features=candidate.features,
            risk_tags=candidate.risk_tags,
            reasons=candidate.reasons,
        )
        for index, (final, opportunity, candidate) in enumerate(scored[:limit], start=1)
    )
