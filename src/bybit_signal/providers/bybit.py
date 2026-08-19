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
    index_price: Decimal | None = Field(default=None, gt=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    high_24h: Decimal = Field(gt=0)
    low_24h: Decimal = Field(gt=0)
    turnover_24h: Decimal = Field(ge=0)
    volume_24h: Decimal = Field(ge=0)
    price_change_24h: Decimal
    funding_rate: Decimal | None = None
    open_interest: Decimal | None = Field(default=None, ge=0)
    open_interest_value: Decimal | None = Field(default=None, ge=0)
    next_funding_time: datetime | None = None
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

    async def completed_candles(
        self,
        symbol: str,
        *,
        timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
        limit: int = 240,
    ) -> tuple[Candle, ...]:
        interval, duration = {
            "1m": ("1", timedelta(minutes=1)),
            "5m": ("5", timedelta(minutes=5)),
            "15m": ("15", timedelta(minutes=15)),
            "30m": ("30", timedelta(minutes=30)),
            "1h": ("60", timedelta(hours=1)),
            "4h": ("240", timedelta(hours=4)),
        }[timeframe]
        result, observed_at = await self._get_result(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        if result.get("symbol") != symbol:
            raise BybitPublicError("Bybit kline symbol does not match request")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit kline list is invalid")
        candles = [_candle(symbol, timeframe, duration, row, observed_at) for row in rows]
        completed = [candle for candle in candles if candle.completed]
        completed.sort(key=lambda candle: candle.open_time)
        return tuple(completed)

    async def recent_candles(
        self,
        symbol: str,
        *,
        timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
        limit: int = 240,
    ) -> tuple[Candle, ...]:
        """Return completed and forming candles with explicit completion state."""

        interval, duration = {
            "1m": ("1", timedelta(minutes=1)),
            "5m": ("5", timedelta(minutes=5)),
            "15m": ("15", timedelta(minutes=15)),
            "30m": ("30", timedelta(minutes=30)),
            "1h": ("60", timedelta(hours=1)),
            "4h": ("240", timedelta(hours=4)),
        }[timeframe]
        result, observed_at = await self._get_result(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        if result.get("symbol") != symbol:
            raise BybitPublicError("Bybit kline symbol does not match request")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit kline list is invalid")
        candles = [_candle(symbol, timeframe, duration, row, observed_at) for row in rows]
        candles.sort(key=lambda candle: candle.open_time)
        return tuple(candles)

    async def completed_5m_candles(self, symbol: str, *, limit: int = 72) -> tuple[Candle, ...]:
        return await self.completed_candles(
            symbol,
            timeframe="5m",
            limit=limit,
        )

    async def orderbook(self, symbol: str, *, limit: int = 50) -> BybitOrderBook:
        result, observed_at = await self._get_result(
            "/v5/market/orderbook",
            {"category": "linear", "symbol": symbol, "limit": limit},
        )
        if result.get("s") != symbol:
            raise BybitPublicError("Bybit orderbook symbol does not match request")
        bids = _book_levels(result.get("b"), "bids")
        asks = _book_levels(result.get("a"), "asks")
        if not bids or not asks or bids[0].price >= asks[0].price:
            raise BybitPublicError("Bybit orderbook is empty or crossed")
        return BybitOrderBook(
            symbol=symbol,
            observed_at=observed_at,
            update_id=_non_negative_int(result.get("u"), "orderbook update id"),
            sequence=_non_negative_int(result.get("seq"), "orderbook sequence"),
            bids=tuple(bids),
            asks=tuple(asks),
        )

    async def recent_trades(
        self, symbol: str, *, limit: int = 1000
    ) -> tuple[BybitPublicTrade, ...]:
        result, _ = await self._get_result(
            "/v5/market/recent-trade",
            {"category": "linear", "symbol": symbol, "limit": limit},
        )
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit public trade list is invalid")
        trades = [_public_trade(symbol, row) for row in rows]
        trades.sort(key=lambda trade: trade.timestamp)
        return tuple(trades)

    async def open_interest_history(
        self,
        symbol: str,
        *,
        interval: Literal["5min", "15min", "30min", "1h", "4h"] = "5min",
        limit: int = 48,
    ) -> tuple[BybitOpenInterest, ...]:
        result, _ = await self._get_result(
            "/v5/market/open-interest",
            {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval,
                "limit": limit,
            },
        )
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit open-interest list is invalid")
        points = [_open_interest(symbol, row) for row in rows]
        points.sort(key=lambda point: point.timestamp)
        return tuple(points)

    async def long_short_ratio(
        self,
        symbol: str,
        *,
        period: Literal["5min", "15min", "30min", "1h", "4h"] = "5min",
        limit: int = 48,
    ) -> tuple[BybitLongShortRatio, ...]:
        result, _ = await self._get_result(
            "/v5/market/account-ratio",
            {
                "category": "linear",
                "symbol": symbol,
                "period": period,
                "limit": limit,
            },
        )
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitPublicError("Bybit long-short ratio list is invalid")
        points = [_long_short_ratio(symbol, row) for row in rows]
        points.sort(key=lambda point: point.timestamp)
        return tuple(points)


class BybitBookLevel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class BybitOrderBook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    observed_at: datetime
    update_id: int = Field(ge=0)
    sequence: int = Field(ge=0)
    bids: tuple[BybitBookLevel, ...] = Field(min_length=1)
    asks: tuple[BybitBookLevel, ...] = Field(min_length=1)


class BybitPublicTrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    trade_id: str = Field(min_length=1, max_length=160)
    timestamp: datetime
    side: Literal["Buy", "Sell"]
    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class BybitOpenInterest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    timestamp: datetime
    open_interest: Decimal = Field(ge=0)


class BybitLongShortRatio(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    timestamp: datetime
    buy_ratio: Decimal = Field(ge=0, le=1)
    sell_ratio: Decimal = Field(ge=0, le=1)

    @property
    def long_short_ratio(self) -> Decimal | None:
        return self.buy_ratio / self.sell_ratio if self.sell_ratio > 0 else None


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
        index_raw = value.get("indexPrice")
        index = _decimal(index_raw, "index price") if index_raw not in {None, ""} else None
        funding_raw = value.get("fundingRate")
        funding = _decimal(funding_raw, "funding rate") if funding_raw not in {None, ""} else None
        open_interest_raw = value.get("openInterest")
        open_interest = (
            _decimal(open_interest_raw, "open interest")
            if open_interest_raw not in {None, ""}
            else None
        )
        open_interest_value_raw = value.get("openInterestValue")
        open_interest_value = (
            _decimal(open_interest_value_raw, "open interest value")
            if open_interest_value_raw not in {None, ""}
            else None
        )
        next_funding_raw = value.get("nextFundingTime")
        next_funding_time = (
            _timestamp(next_funding_raw, "next funding time")
            if next_funding_raw not in {None, "", "0", 0}
            else None
        )
        return BybitTicker(
            symbol=symbol,
            last_price=_decimal(value.get("lastPrice"), "last price"),
            mark_price=mark,
            index_price=index,
            bid_price=_decimal(value.get("bid1Price"), "best bid"),
            ask_price=_decimal(value.get("ask1Price"), "best ask"),
            high_24h=_decimal(value.get("highPrice24h"), "24h high"),
            low_24h=_decimal(value.get("lowPrice24h"), "24h low"),
            turnover_24h=_decimal(value.get("turnover24h"), "24h turnover"),
            volume_24h=_decimal(value.get("volume24h"), "24h volume"),
            price_change_24h=_decimal(value.get("price24hPcnt"), "24h price change"),
            funding_rate=funding,
            open_interest=open_interest,
            open_interest_value=open_interest_value,
            next_funding_time=next_funding_time,
            observed_at=observed_at,
        )
    except (ValueError, TypeError):
        return None


def _candle(
    symbol: str,
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
    duration: timedelta,
    value: object,
    observed_at: datetime,
) -> Candle:
    if not isinstance(value, list) or len(value) < 7:
        raise BybitPublicError("Bybit kline row is invalid")
    open_time = _timestamp(value[0], "kline start")
    close_time = open_time + duration
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
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


def _book_levels(value: object, field: str) -> list[BybitBookLevel]:
    if not isinstance(value, list):
        raise BybitPublicError(f"Bybit orderbook {field} are invalid")
    levels: list[BybitBookLevel] = []
    for row in value:
        if not isinstance(row, list) or len(row) < 2:
            raise BybitPublicError(f"Bybit orderbook {field} row is invalid")
        levels.append(
            BybitBookLevel(
                price=_decimal(row[0], f"{field} price"),
                size=_decimal(row[1], f"{field} size"),
            )
        )
    return levels


def _public_trade(symbol: str, value: object) -> BybitPublicTrade:
    if not isinstance(value, dict):
        raise BybitPublicError("Bybit public trade row is invalid")
    side = value.get("side")
    trade_id = value.get("execId")
    if side not in {"Buy", "Sell"} or not isinstance(trade_id, str):
        raise BybitPublicError("Bybit public trade identity is invalid")
    return BybitPublicTrade(
        symbol=symbol,
        trade_id=trade_id,
        timestamp=_timestamp(value.get("time"), "public trade time"),
        side=side,
        price=_decimal(value.get("price"), "public trade price"),
        size=_decimal(value.get("size"), "public trade size"),
    )


def _open_interest(symbol: str, value: object) -> BybitOpenInterest:
    if not isinstance(value, dict):
        raise BybitPublicError("Bybit open-interest row is invalid")
    return BybitOpenInterest(
        symbol=symbol,
        timestamp=_timestamp(value.get("timestamp"), "open-interest time"),
        open_interest=_decimal(value.get("openInterest"), "open interest"),
    )


def _long_short_ratio(symbol: str, value: object) -> BybitLongShortRatio:
    if not isinstance(value, dict):
        raise BybitPublicError("Bybit long-short ratio row is invalid")
    return BybitLongShortRatio(
        symbol=symbol,
        timestamp=_timestamp(value.get("timestamp"), "long-short ratio time"),
        buy_ratio=_decimal(value.get("buyRatio"), "buy ratio"),
        sell_ratio=_decimal(value.get("sellRatio"), "sell ratio"),
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


def _non_negative_int(value: object, field: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is invalid") from error
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result


def _timestamp(value: object, field: str) -> datetime:
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is invalid") from error
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
