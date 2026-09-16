from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from analysis_core.market import Markets, aggregate_ten_minutes
from analysis_core.vendor.models import Candle
from analysis_core.vendor.public import BybitPublicClient


def candle(at, timeframe="5m", minutes=5, completed=True):
    return Candle(
        symbol="TESTUSDT",
        timeframe=timeframe,
        open_time=at,
        close_time=at + timedelta(minutes=minutes),
        open=D("10"),
        high=D("12"),
        low=D("9"),
        close=D("11"),
        volume=D("3.2"),
        turnover=D("32"),
        completed=completed,
        source="BYBIT",
    )


def test_ten_minute_utc_aggregation_and_forming_state():
    at = datetime(2026, 9, 9, 8, tzinfo=UTC)
    result = aggregate_ten_minutes(
        [
            candle(at),
            candle(at + timedelta(minutes=5)),
            candle(at + timedelta(minutes=10), completed=False),
        ]
    )
    assert len(result) == 2 and result[0].completed and not result[1].completed
    assert result[0].volume == D("6.4") and result[0].turnover == 64
    assert result[0].timeframe == "10m" and result[1].open_time == at + timedelta(minutes=10)
    with pytest.raises(ValueError):
        aggregate_ten_minutes([candle(at), candle(at + timedelta(minutes=10))])


async def test_evidence_has_all_requested_timeframes():
    class Client:
        async def recent_candles(self, symbol, timeframe, limit):
            minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120}[
                timeframe
            ]
            now = datetime.now(UTC)
            start = datetime.fromtimestamp(
                int(now.timestamp()) // (minutes * 60) * (minutes * 60), UTC
            )
            return tuple(
                candle(
                    start - timedelta(minutes=minutes * (limit - 1 - i)),
                    timeframe,
                    minutes,
                    i < limit - 1,
                )
                for i in range(limit)
            )

    result = await Markets(Client(), include_orderflow=False).evidence("TESTUSDT", {})
    assert set(result["candles"]) == {"1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h"}
    assert result["timeframe_roles"]["primary"] == ["15m", "30m", "1h", "2h"]
    assert result["timeframe_roles"]["secondary"] == ["1m", "3m", "5m", "10m"]
    assert set(result["indicators"]) == set(result["candles"])
    for tf, indicators in result["indicators"].items():
        closed = [c for c in result["candles"][tf] if c["completed"]]
        assert datetime.fromisoformat(
            indicators["recent"][-1]["closed_at"]
        ) == datetime.fromisoformat(closed[-1]["close_time"])
        assert indicators["closed_samples"] == len(closed)
    assert len(result["candles"]["2h"]) == 85
    assert len(result["candles"]["1h"]) == 169
    assert len(result["candles"]["30m"]) == 337
    assert len(result["candles"]["5m"]) == 145
    assert 72 <= len(result["candles"]["10m"]) <= 73
    for tf in ("30m", "1h", "2h"):
        candles = result["candles"][tf]
        start = datetime.fromisoformat(candles[0]["open_time"])
        end = datetime.fromisoformat(candles[-1]["open_time"])
        assert end - start == timedelta(days=7)


async def test_two_hour_native_request_uses_120():
    async def handler(request):
        assert request.url.params["interval"] == "120"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "symbol": "TESTUSDT",
                    "list": [["1788912000000", "10", "12", "9", "11", "3", "30"]],
                },
                "time": 1788919200000,
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)
    )
    try:
        result = await BybitPublicClient(client=client).recent_candles("TESTUSDT", timeframe="2h")
        assert result[0].timeframe == "2h"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "change",
    [
        {"high": D("9")},
        {"volume": D("-1")},
        {"symbol": "OTHERUSDT"},
        {"completed": "true"},
        {"open": D("Infinity")},
    ],
)
def test_host_rejects_malformed_candle_even_if_model_was_bypassed(change):
    from analysis_core.market import validate_direction_rows

    at = datetime(2026, 9, 9, 8, tzinfo=UTC)
    row = candle(at).model_copy(update=change)
    with pytest.raises(ValueError):
        validate_direction_rows([row], "TESTUSDT", "5m", 5, at + timedelta(minutes=10))


def test_host_accepts_valid_zero_volume():
    from analysis_core.market import validate_direction_rows

    at = datetime(2026, 9, 9, 8, tzinfo=UTC)
    row = candle(at).model_copy(update={"volume": D(0), "turnover": D(0)})
    validate_direction_rows([row], "TESTUSDT", "5m", 5, at + timedelta(minutes=10))


def test_historical_incomplete_candle_rejected():
    from analysis_core.market import validate_direction_rows

    at = datetime(2026, 9, 9, 8, tzinfo=UTC)
    with pytest.raises(ValueError):
        validate_direction_rows(
            [candle(at, completed=False), candle(at + timedelta(minutes=5))],
            "TESTUSDT",
            "5m",
            5,
            at + timedelta(minutes=10),
        )


@pytest.mark.parametrize("short_tf", ["15m", "30m", "1h", "2h"])
async def test_one_missing_closed_bar_fails_week_gate(short_tf):
    class Client:
        async def recent_candles(self, symbol, timeframe, limit):
            minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120}[
                timeframe
            ]
            count = limit - 1 if timeframe == short_tf else limit
            at = datetime.fromtimestamp(
                int(datetime.now(UTC).timestamp()) // (minutes * 60) * minutes * 60, UTC
            )
            return tuple(
                candle(
                    at - timedelta(minutes=minutes * (count - 1 - i)),
                    timeframe,
                    minutes,
                    i < count - 1,
                )
                for i in range(count)
            )

    with pytest.raises(ValueError, match="SKIP_INSUFFICIENT_WEEK_HISTORY"):
        await Markets(Client()).evidence("TESTUSDT", {})


async def test_recent_candles_paginates_without_overlaps():
    end = datetime(2026, 9, 11, tzinfo=UTC)
    calls = []

    async def handler(request):
        count = int(request.url.params["limit"])
        calls.append(count)
        end_ms = int(request.url.params.get("end", int(end.timestamp() * 1000)))
        latest = end_ms // 60000 * 60000
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "time": int(end.timestamp() * 1000),
                "result": {
                    "symbol": "TESTUSDT",
                    "list": [
                        [str(latest - i * 60000), "10", "12", "9", "11", "3", "30"]
                        for i in range(count)
                    ],
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.bybit.com", transport=httpx.MockTransport(handler)
    ) as http:
        rows = await BybitPublicClient(client=http).recent_candles(
            "TESTUSDT", timeframe="1m", limit=1441
        )
    assert calls == [1000, 441]
    assert len(rows) == 1441
    assert all(
        b.open_time - a.open_time == timedelta(minutes=1)
        for a, b in zip(rows, rows[1:], strict=False)
    )


def test_direction_context_exposes_exact_last_closed_without_using_forming():
    from analysis_core.model import direction_context

    at = datetime(2026, 9, 15, tzinfo=UTC)
    closed = candle(at).model_dump(mode="json")
    forming = candle(at + timedelta(minutes=5), completed=False).model_dump(mode="json")
    forming["close"] = "999"
    result = direction_context({"symbol": "TESTUSDT", "candles": {"5m": [closed, forming]}})
    assert result["latest_closed_candles"]["5m"] == closed
    assert result["latest_closed_candles"]["5m"]["close"] != "999"
