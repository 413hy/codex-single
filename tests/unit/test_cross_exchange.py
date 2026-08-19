from __future__ import annotations

import json
from typing import Any

import httpx

from bybit_signal.providers.cross_exchange import CrossExchangePublicClient


class _ClientFactory:
    def __init__(self) -> None:
        self.clients: list[httpx.AsyncClient] = []

    def __call__(self, **kwargs: Any) -> httpx.AsyncClient:
        base_url = str(kwargs["base_url"])

        async def handler(request: httpx.Request) -> httpx.Response:
            if "binance" in base_url and request.url.path.endswith("/ticker/24hr"):
                payload: dict[str, Any] = {
                    "symbol": "BTCUSDT",
                    "lastPrice": "64200",
                    "bidPrice": None,
                    "askPrice": None,
                    "closeTime": 1_787_039_300_000,
                }
            elif "binance" in base_url and request.url.path.endswith("/ticker/bookTicker"):
                payload = {
                    "symbol": "BTCUSDT",
                    "bidPrice": "64199",
                    "askPrice": "64201",
                    "time": 1_787_039_301_000,
                }
            else:
                payload = {"code": "51001", "data": []}
            return httpx.Response(200, content=json.dumps(payload).encode(), request=request)

        client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )
        self.clients.append(client)
        return client


async def test_binance_reference_uses_dedicated_book_ticker_for_bid_and_ask() -> None:
    factory = _ClientFactory()
    client = CrossExchangePublicClient(
        binance_base_url="https://binance.test",
        okx_base_url="https://okx.test",
        client_factory=factory,  # type: ignore[arg-type]
    )
    try:
        tickers, failures = await client.tickers("BTCUSDT")
    finally:
        await client.close()

    assert len(tickers) == 1
    assert tickers[0].exchange == "BINANCE"
    assert str(tickers[0].bid_price) == "64199"
    assert str(tickers[0].ask_price) == "64201"
    assert "reference.okx" in failures
