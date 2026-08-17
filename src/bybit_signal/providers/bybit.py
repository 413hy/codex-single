from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from bybit_signal.domain.models import Candle, Symbol


class BybitPublicError(RuntimeError):
    """Raised when Bybit public market data cannot be validated."""


class BybitInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    base_coin: str
    quote_coin: Literal["USDT"]
    settle_coin: Literal["USDT"]
    contract_type: str
    status: str
    launch_time: datetime


class BybitTicker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    last_price: Decimal = Field(gt=0)
    mark_price: Decimal | None = Field(default=None, gt=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    high_24h: Decimal = Field(gt=0)
    low_24h: Decimal = Field(gt=0)
    turnover_24h: Decimal = Field(ge=0)
    volume_24h: Decimal = Field(ge=0)
    price_change_24h: Decimal
    observed_at: datetime

    @property
    def spread_bps(self) -> Decimal:
        midpoint = (self.bid_price + self.ask_price) / 2
        return (self.ask_price - self.bid_price) / midpoint * Decimal(10_000)

    @property
    def range_24h_percent(self) -> Decimal:
        return (self.high_24h - self.low_24h) / self.last_price * Decimal(100)


class BybitPublicClient:
    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15,
        max_attempts: int = 3,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "bybit-multi-source-signal/0.1"},
        )
        self._max_attempts = max_attempts

    async def __aenter__(self) -> BybitPublicClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_result(
        self, path: str, params: dict[str, str | int]
    ) -> tuple[dict[str, Any], datetime]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict):
                    raise BybitPublicError("Bybit response root is not an object")
                if document.get("retCode") != 0:
                    raise BybitPublicError(
                        f"Bybit rejected public request: {document.get('retMsg', 'unknown')}"
                    )
                result = document.get("result")
                if not isinstance(result, dict):
                    raise BybitPublicError("Bybit result is not an object")
                server_time = _timestamp(document.get("time"), "response time")
                return result, server_time
            except (httpx.HTTPError, ValueError, BybitPublicError) as error:
                last_error = error
                if attempt < self._max_attempts:
                    await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
        raise BybitPublicError("Bybit public request failed") from last_error

    async def instruments(self) -> tuple[BybitInstrument, ...]:
        instruments: list[BybitInstrument] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(20):
            params: dict[str, str | int] = {
                "category": "linear",
                "status": "Trading",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            result, _ = await self._get_result("/v5/market/instruments-info", params)
            rows = result.get("list")
            if not isinstance(rows, list):
                raise BybitPublicError("Bybit instrument list is invalid")
            for row in rows:
                instrument = _instrument(row)
                if instrument is not None:
                    instruments.append(instrument)
            next_cursor = result.get("nextPageCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise BybitPublicError("Bybit instrument pagination repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise BybitPublicError("Bybit instrument pagination exceeded the safety limit")
        unique = {instrument.symbol: instrument for instrument in instruments}
        return tuple(unique[symbol] for symbol in sorted(unique))

    async def tickers(self) -> dict[str, BybitTicker]:
        result, observed_at = await self._get_result("/v5/market/tickers", {"category": "linear"})
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit ticker list is invalid")
        tickers: dict[str, BybitTicker] = {}
        for row in rows:
            ticker = _ticker(row, observed_at)
            if ticker is not None:
                tickers[ticker.symbol] = ticker
        return tickers

    async def completed_5m_candles(self, symbol: str, *, limit: int = 72) -> tuple[Candle, ...]:
        result, observed_at = await self._get_result(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": "5", "limit": limit},
        )
        if result.get("symbol") != symbol:
            raise BybitPublicError("Bybit kline symbol does not match request")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit kline list is invalid")
        candles = [_candle(symbol, row, observed_at) for row in rows]
        completed = [candle for candle in candles if candle.completed]
        completed.sort(key=lambda candle: candle.open_time)
        return tuple(completed)


def _instrument(value: object) -> BybitInstrument | None:
    if not isinstance(value, dict):
        return None
    if (
        value.get("status") != "Trading"
        or value.get("contractType") != "LinearPerpetual"
        or value.get("quoteCoin") != "USDT"
        or value.get("settleCoin") != "USDT"
    ):
        return None
    symbol = value.get("symbol")
    base_coin = value.get("baseCoin")
    if not isinstance(symbol, str) or not isinstance(base_coin, str) or not symbol.endswith("USDT"):
        return None
    try:
        return BybitInstrument(
            symbol=symbol,
            base_coin=base_coin,
            quote_coin="USDT",
            settle_coin="USDT",
            contract_type="LinearPerpetual",
            status="Trading",
            launch_time=_timestamp(value.get("launchTime"), "launch time"),
        )
    except (ValueError, TypeError):
        return None


def _ticker(value: object, observed_at: datetime) -> BybitTicker | None:
    if not isinstance(value, dict):
        return None
    symbol = value.get("symbol")
    if not isinstance(symbol, str) or not symbol.endswith("USDT"):
        return None
    try:
        mark_raw = value.get("markPrice")
        mark = _decimal(mark_raw, "mark price") if mark_raw not in {None, ""} else None
        return BybitTicker(
            symbol=symbol,
            last_price=_decimal(value.get("lastPrice"), "last price"),
            mark_price=mark,
            bid_price=_decimal(value.get("bid1Price"), "best bid"),
            ask_price=_decimal(value.get("ask1Price"), "best ask"),
            high_24h=_decimal(value.get("highPrice24h"), "24h high"),
            low_24h=_decimal(value.get("lowPrice24h"), "24h low"),
            turnover_24h=_decimal(value.get("turnover24h"), "24h turnover"),
            volume_24h=_decimal(value.get("volume24h"), "24h volume"),
            price_change_24h=_decimal(value.get("price24hPcnt"), "24h price change"),
            observed_at=observed_at,
        )
    except (ValueError, TypeError):
        return None


def _candle(symbol: str, value: object, observed_at: datetime) -> Candle:
    if not isinstance(value, list) or len(value) < 7:
        raise BybitPublicError("Bybit kline row is invalid")
    open_time = _timestamp(value[0], "kline start")
    close_time = open_time + timedelta(minutes=5)
    return Candle(
        symbol=symbol,
        timeframe="5m",
        open_time=open_time,
        close_time=close_time,
        open=_decimal(value[1], "kline open"),
        high=_decimal(value[2], "kline high"),
        low=_decimal(value[3], "kline low"),
        close=_decimal(value[4], "kline close"),
        volume=_decimal(value[5], "kline volume"),
        turnover=_decimal(value[6], "kline turnover"),
        completed=close_time <= observed_at,
        source="BYBIT",
    )


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} is not numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} is invalid") from error
    if not result.is_finite():
        raise ValueError(f"{field} is not finite")
    return result


def _timestamp(value: object, field: str) -> datetime:
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is invalid") from error
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
