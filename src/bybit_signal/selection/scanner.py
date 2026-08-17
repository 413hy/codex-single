from __future__ import annotations

import asyncio
import itertools
import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from bybit_signal.config import ScannerConfig
from bybit_signal.domain.models import Candle, Symbol
from bybit_signal.providers.bybit import BybitInstrument, BybitPublicClient, BybitTicker


class CandidateFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    median_range_percent: float = Field(ge=0)
    realized_volatility_percent: float = Field(ge=0)
    maximum_absolute_return_percent: float = Field(ge=0)
    direction_efficiency: float = Field(ge=0, le=1)
    recent_turnover_ratio: float = Field(ge=0)
    spread_bps: float = Field(ge=0)
    turnover_24h_usdt: float = Field(ge=0)
    completed_candles: int = Field(ge=0)
    missing_intervals: int = Field(ge=0)


class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1)
    symbol: Symbol
    score: float = Field(ge=0, le=100)
    last_price: Decimal = Field(gt=0)
    observed_at: datetime
    features: CandidateFeatures
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
    raw_score: float = Field(ge=0)
    ticker: BybitTicker
    features: CandidateFeatures
    reasons: tuple[str, ...]


class BybitUniverseScanner:
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
                    return self._candidate(ticker, candles)
                except Exception as error:  # one symbol must not fail the universe scan
                    failures[instrument.symbol] = type(error).__name__
                    return None

        analyzed = await asyncio.gather(
            *(analyze(instrument, ticker) for instrument, ticker in selected)
        )
        unranked = [candidate for candidate in analyzed if candidate is not None]
        unranked.sort(key=lambda candidate: candidate.raw_score, reverse=True)
        ranked = _normalize_rankings(unranked, limit)
        return ScanResult(
            generated_at=datetime.now(UTC),
            universe_size=len(instruments),
            ticker_eligible_size=len(eligible),
            kline_analyzed_size=len(selected) - len(failures),
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
        self, ticker: BybitTicker, candles: tuple[Candle, ...]
    ) -> _UnrankedCandidate | None:
        if len(candles) < self._config.minimum_completed_candles:
            return None
        candles = candles[-self._config.candle_limit :]
        missing_intervals = _missing_intervals(candles)
        if missing_intervals > self._config.maximum_missing_intervals:
            return None

        closes = [float(candle.close) for candle in candles]
        ranges = [float((candle.high - candle.low) / candle.close * 100) for candle in candles]
        returns = [(closes[index] / closes[index - 1] - 1) * 100 for index in range(1, len(closes))]
        median_range = statistics.median(ranges)
        maximum_return = max(abs(value) for value in returns)
        if (
            median_range < self._config.minimum_median_range_percent
            and maximum_return < self._config.minimum_max_return_percent
        ):
            return None
        realized = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        total_path = sum(abs(value) for value in returns)
        efficiency = abs((closes[-1] / closes[0] - 1) * 100) / total_path if total_path else 0.0
        recent_turnover = sum(float(candle.turnover) for candle in candles[-6:])
        previous_turnover = sum(float(candle.turnover) for candle in candles[-12:-6])
        turnover_ratio = recent_turnover / previous_turnover if previous_turnover > 0 else 0.0
        features = CandidateFeatures(
            median_range_percent=median_range,
            realized_volatility_percent=realized,
            maximum_absolute_return_percent=maximum_return,
            direction_efficiency=min(1.0, efficiency),
            recent_turnover_ratio=max(0.0, turnover_ratio),
            spread_bps=float(ticker.spread_bps),
            turnover_24h_usdt=float(ticker.turnover_24h),
            completed_candles=len(candles),
            missing_intervals=missing_intervals,
        )
        raw_score = (
            median_range * 0.30
            + realized * 0.30
            + maximum_return * 0.22
            + min(3.0, turnover_ratio) * 0.10
            + min(1.0, efficiency) * 0.08
        )
        reasons = (
            f"5m中位振幅 {median_range:.3f}%",
            f"5m实现波动 {realized:.3f}%",
            f"最大单根变动 {maximum_return:.3f}%",
            f"近30m成交额比 {turnover_ratio:.2f}x",
            f"买卖价差 {float(ticker.spread_bps):.2f} bps",
        )
        return _UnrankedCandidate(
            symbol=ticker.symbol,
            raw_score=max(0.0, raw_score),
            ticker=ticker,
            features=features,
            reasons=reasons,
        )


def _missing_intervals(candles: tuple[Candle, ...]) -> int:
    expected = timedelta(minutes=5)
    missing = 0
    for previous, current in itertools.pairwise(candles):
        gap = current.open_time - previous.open_time
        if gap > expected:
            missing += max(0, round(gap / expected) - 1)
    return missing


def _normalize_rankings(
    candidates: list[_UnrankedCandidate], limit: int
) -> tuple[RankedCandidate, ...]:
    if not candidates or limit <= 0:
        return ()
    maximum = candidates[0].raw_score
    minimum = candidates[-1].raw_score
    spread = maximum - minimum
    ranked: list[RankedCandidate] = []
    for index, candidate in enumerate(candidates[:limit], start=1):
        score = 100.0 if spread == 0 else (candidate.raw_score - minimum) / spread * 100
        ranked.append(
            RankedCandidate(
                rank=index,
                symbol=candidate.symbol,
                score=round(score, 3),
                last_price=candidate.ticker.last_price,
                observed_at=candidate.ticker.observed_at,
                features=candidate.features,
                reasons=candidate.reasons,
            )
        )
    return tuple(ranked)
