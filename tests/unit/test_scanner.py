from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from bybit_signal.config import ScannerConfig
from bybit_signal.domain.models import Candle
from bybit_signal.providers.bybit import BybitInstrument, BybitPublicClient, BybitTicker
from bybit_signal.selection.scanner import BybitUniverseScanner


class FakeBybitClient:
    def __init__(
        self,
        instruments: tuple[BybitInstrument, ...],
        tickers: dict[str, BybitTicker],
        candles: dict[str, tuple[Candle, ...]],
    ) -> None:
        self._instruments = instruments
        self._tickers = tickers
        self._candles = candles

    async def instruments(self) -> tuple[BybitInstrument, ...]:
        return self._instruments

    async def tickers(self) -> dict[str, BybitTicker]:
        return self._tickers

    async def completed_5m_candles(self, symbol: str, *, limit: int = 72) -> tuple[Candle, ...]:
        return self._candles[symbol][-limit:]


def _instrument(symbol: str, now: datetime) -> BybitInstrument:
    return BybitInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        launch_time=now - timedelta(days=30),
    )


def _ticker(symbol: str, now: datetime, *, last: str, high: str, low: str) -> BybitTicker:
    value = Decimal(last)
    return BybitTicker(
        symbol=symbol,
        last_price=value,
        mark_price=value,
        bid_price=value * Decimal("0.9999"),
        ask_price=value * Decimal("1.0001"),
        high_24h=Decimal(high),
        low_24h=Decimal(low),
        turnover_24h=Decimal("5000000"),
        volume_24h=Decimal("10000000"),
        price_change_24h=Decimal("0.05"),
        observed_at=now,
    )


def _candles(
    symbol: str,
    now: datetime,
    moves: list[Decimal],
    *,
    turnover_start: Decimal = Decimal("10000"),
    turnover_step: Decimal = Decimal("200"),
) -> tuple[Candle, ...]:
    rows = []
    price = Decimal("1")
    start = now - timedelta(minutes=5 * len(moves))
    for index, move in enumerate(moves):
        close = price * (Decimal(1) + move)
        high = max(price, close) * Decimal("1.002")
        low = min(price, close) * Decimal("0.998")
        rows.append(
            Candle(
                symbol=symbol,
                timeframe="5m",
                open_time=start + timedelta(minutes=5 * index),
                close_time=start + timedelta(minutes=5 * (index + 1)),
                open=price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000"),
                turnover=turnover_start + Decimal(index) * turnover_step,
                completed=True,
                source="BYBIT",
            )
        )
        price = close
    return tuple(rows)


@pytest.mark.asyncio
async def test_scanner_ranks_completed_5m_volatility_without_trade_sizing() -> None:
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    symbols = ("CYSUSDT", "HUSDT")
    fake = FakeBybitClient(
        tuple(_instrument(symbol, now) for symbol in symbols),
        {
            "CYSUSDT": _ticker("CYSUSDT", now, last="1", high="1.4", low="0.7"),
            "HUSDT": _ticker("HUSDT", now, last="1", high="1.2", low="0.8"),
        },
        {
            "CYSUSDT": _candles(
                "CYSUSDT",
                now,
                [Decimal("0.01") if index % 2 == 0 else Decimal("-0.008") for index in range(60)],
            ),
            "HUSDT": _candles(
                "HUSDT",
                now,
                [Decimal("0.003") if index % 2 == 0 else Decimal("-0.002") for index in range(60)],
            ),
        },
    )
    config = ScannerConfig(preselect_limit=10, minimum_completed_candles=48)
    scanner = BybitUniverseScanner(cast(BybitPublicClient, fake), config)
    result = await scanner.scan(limit=2)

    assert result.universe_size == 2
    assert [candidate.symbol for candidate in result.candidates] == ["CYSUSDT", "HUSDT"]
    assert result.candidates[0].score == 100
    assert all(
        "杠杆" not in reason and "利润" not in reason
        for candidate in result.candidates
        for reason in candidate.reasons
    )
    assert result.candidates[0].features.recent_30m_turnover_usdt >= 50_000


@pytest.mark.asyncio
async def test_scanner_filters_low_absolute_recent_turnover() -> None:
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    moves = [Decimal("0.01") if index % 2 == 0 else Decimal("-0.008") for index in range(60)]
    fake = FakeBybitClient(
        (_instrument("ACTIVEUSDT", now), _instrument("COLDUSDT", now)),
        {
            "ACTIVEUSDT": _ticker("ACTIVEUSDT", now, last="1", high="1.4", low="0.7"),
            "COLDUSDT": _ticker("COLDUSDT", now, last="1", high="1.4", low="0.7"),
        },
        {
            "ACTIVEUSDT": _candles("ACTIVEUSDT", now, moves),
            "COLDUSDT": _candles(
                "COLDUSDT",
                now,
                moves,
                turnover_start=Decimal("100"),
                turnover_step=Decimal("1"),
            ),
        },
    )

    result = await BybitUniverseScanner(
        cast(BybitPublicClient, fake),
        ScannerConfig(preselect_limit=10),
    ).scan(limit=5)

    assert [candidate.symbol for candidate in result.candidates] == ["ACTIVEUSDT"]


@pytest.mark.asyncio
async def test_scanner_rejects_candle_gaps_beyond_tolerance() -> None:
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    candles = list(_candles("CYSUSDT", now, [Decimal("0.01")] * 60))
    candles.pop(20)
    candles.pop(21)
    candles.pop(22)
    fake = FakeBybitClient(
        (_instrument("CYSUSDT", now),),
        {"CYSUSDT": _ticker("CYSUSDT", now, last="1", high="1.4", low="0.7")},
        {"CYSUSDT": tuple(candles)},
    )
    scanner = BybitUniverseScanner(
        cast(BybitPublicClient, fake),
        ScannerConfig(preselect_limit=10, maximum_missing_intervals=2),
    )
    result = await scanner.scan(limit=5)

    assert result.candidates == ()
