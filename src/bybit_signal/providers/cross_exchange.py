from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from bybit_signal.domain.models import Symbol


class ReferenceTicker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: Symbol
    exchange: Literal["BINANCE", "OKX"]
    instrument: str = Field(min_length=3, max_length=80)
    last_price: Decimal = Field(gt=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    observed_at: datetime

    @property
    def spread_bps(self) -> Decimal:
        midpoint = (self.bid_price + self.ask_price) / 2
        return (self.ask_price - self.bid_price) / midpoint * Decimal(10_000)


class CrossExchangePublicClient:
    """Minimal public ticker adapters; reference prices never become canonical."""

    def __init__(
        self,
        *,
        binance_base_url: str = "https://fapi.binance.com",
        okx_base_url: str = "https://www.okx.com",
        timeout_seconds: float = 10,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        headers = {"User-Agent": "bybit-native-signal/0.1"}
        self._binance = client_factory(
            base_url=binance_base_url, timeout=timeout_seconds, headers=headers
        )
        self._okx = client_factory(base_url=okx_base_url, timeout=timeout_seconds, headers=headers)

    async def close(self) -> None:
        await asyncio.gather(self._binance.aclose(), self._okx.aclose())

    async def tickers(
        self, symbol: str
    ) -> tuple[tuple[ReferenceTicker, ...], dict[str, str]]:
        results = await asyncio.gather(
            self._capture("BINANCE", self._binance_ticker(symbol)),
            self._capture("OKX", self._okx_ticker(symbol)),
        )
        tickers: list[ReferenceTicker] = []
        failures: dict[str, str] = {}
        for source, ticker, error in results:
            if ticker is not None:
                tickers.append(ticker)
            elif error is not None:
                failures[f"reference.{source.lower()}"] = error
        return tuple(tickers), failures

    @staticmethod
    async def _capture(
        source: str,
        operation: Any,
    ) -> tuple[str, ReferenceTicker | None, str | None]:
        try:
            return source, await operation, None
        except Exception as error:
            return source, None, f"{type(error).__name__}: {error}"

    async def _binance_ticker(self, symbol: str) -> ReferenceTicker:
        document = await self._get_json(
            self._binance,
            "/fapi/v1/ticker/24hr",
            {"symbol": symbol},
        )
        if not isinstance(document, dict) or document.get("symbol") != symbol:
            raise ValueError("Binance ticker identity does not match")
        return ReferenceTicker(
            symbol=symbol,
            exchange="BINANCE",
            instrument=symbol,
            last_price=_decimal(document.get("lastPrice"), "Binance last"),
            bid_price=_decimal(document.get("bidPrice"), "Binance bid"),
            ask_price=_decimal(document.get("askPrice"), "Binance ask"),
            observed_at=_timestamp(document.get("closeTime"), "Binance close time"),
        )

    async def _okx_ticker(self, symbol: str) -> ReferenceTicker:
        instrument = f"{symbol.removesuffix('USDT')}-USDT-SWAP"
        document = await self._get_json(
            self._okx,
            "/api/v5/market/ticker",
            {"instId": instrument},
        )
        if not isinstance(document, dict) or document.get("code") != "0":
            raise ValueError("OKX rejected public ticker request")
        data = document.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("OKX ticker payload is unavailable")
        row = data[0]
        if row.get("instId") != instrument:
            raise ValueError("OKX ticker identity does not match")
        return ReferenceTicker(
            symbol=symbol,
            exchange="OKX",
            instrument=instrument,
            last_price=_decimal(row.get("last"), "OKX last"),
            bid_price=_decimal(row.get("bidPx"), "OKX bid"),
            ask_price=_decimal(row.get("askPx"), "OKX ask"),
            observed_at=_timestamp(row.get("ts"), "OKX timestamp"),
        )

    @staticmethod
    async def _get_json(
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str],
    ) -> object:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt == 0:
                    await asyncio.sleep(0.2)
        raise ValueError("public reference ticker request failed") from last_error


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
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
