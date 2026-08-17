from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from bybit_signal.providers.bybit import BybitPublicClient


def _response(result: dict[str, Any], timestamp: datetime) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": result,
            "time": int(timestamp.timestamp() * 1000),
        },
    )


@pytest.mark.asyncio
async def test_instruments_follow_pagination_and_keep_only_usdt_perpetuals() -> None:
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return _response(
                {
                    "list": [
                        {
                            "symbol": "CYSUSDT",
                            "baseCoin": "CYS",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "launchTime": str(int(now.timestamp() * 1000)),
                        },
                        {
                            "symbol": "BTCUSDC",
                            "baseCoin": "BTC",
                            "quoteCoin": "USDC",
                            "settleCoin": "USDC",
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "launchTime": str(int(now.timestamp() * 1000)),
                        },
                    ],
                    "nextPageCursor": "page-2",
                },
                now,
            )
        assert cursor == "page-2"
        return _response(
            {
                "list": [
                    {
                        "symbol": "HUSDT",
                        "baseCoin": "H",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "contractType": "LinearPerpetual",
                        "status": "Trading",
                        "launchTime": str(int(now.timestamp() * 1000)),
                    }
                ],
                "nextPageCursor": "",
            },
            now,
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)
    )
    client = BybitPublicClient(client=http_client, max_attempts=1)
    instruments = await client.instruments()
    await http_client.aclose()

    assert [instrument.symbol for instrument in instruments] == ["CYSUSDT", "HUSDT"]


@pytest.mark.asyncio
async def test_rest_kline_excludes_current_forming_candle_and_sorts_ascending() -> None:
    start = datetime(2026, 8, 17, 0, tzinfo=UTC)
    rows = []
    for index in range(60):
        open_time = start + timedelta(minutes=5 * index)
        rows.append(
            [
                str(int(open_time.timestamp() * 1000)),
                "0.6000",
                "0.6100",
                "0.5900",
                "0.6050",
                "1000",
                "605",
            ]
        )
    observed_at = start + timedelta(minutes=5 * 59 + 2)

    def handler(_: httpx.Request) -> httpx.Response:
        return _response({"symbol": "CYSUSDT", "list": list(reversed(rows))}, observed_at)

    http_client = httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)
    )
    client = BybitPublicClient(client=http_client, max_attempts=1)
    candles = await client.completed_5m_candles("CYSUSDT", limit=60)
    await http_client.aclose()

    assert len(candles) == 59
    assert all(candle.completed for candle in candles)
    assert candles[0].open_time == start
    assert candles[-1].open_time == start + timedelta(minutes=5 * 58)


@pytest.mark.asyncio
async def test_ticker_keeps_last_and_mark_prices_separate() -> None:
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return _response(
            {
                "list": [
                    {
                        "symbol": "CYSUSDT",
                        "lastPrice": "0.6041",
                        "markPrice": "0.6038",
                        "bid1Price": "0.6040",
                        "ask1Price": "0.6042",
                        "highPrice24h": "0.7000",
                        "lowPrice24h": "0.5500",
                        "turnover24h": "5000000",
                        "volume24h": "8000000",
                        "price24hPcnt": "-0.08",
                    }
                ]
            },
            now,
        )

    http_client = httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)
    )
    client = BybitPublicClient(client=http_client, max_attempts=1)
    ticker = (await client.tickers())["CYSUSDT"]
    await http_client.aclose()

    assert str(ticker.last_price) == "0.6041"
    assert str(ticker.mark_price) == "0.6038"
    assert ticker.spread_bps > 0
