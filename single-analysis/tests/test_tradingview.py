import copy
import json
import time
from email.utils import formatdate
from unittest.mock import AsyncMock

import httpx
import pytest

from analysis_core.config import Settings
from analysis_core.model import DirectionModel, direction_context
from analysis_core.store import Store
from analysis_core.tradingview import (
    BASE,
    CONTEXT_METRICS,
    IDENTITY,
    METRICS,
    PERIODS,
    RANK_COLUMNS,
    RATINGS,
    TradingViewError,
    TradingViewWeb,
    extract_ideas,
    field,
    normalize,
    normalize_rankings,
)


def snapshot(now):
    raw = {"name": "BTCUSDT.P", "exchange": "BYBIT", "type": "swap",
           "typespecs": ["crypto", "perpetual"], "update_mode": "streaming"}
    for interval in PERIODS.values():
        duration = int(interval or 1440) * 60
        values = dict.fromkeys(METRICS, 50)
        values.update(time=int(now) // duration * duration, open=100, high=110,
                      low=90, close=105, volume=10)
        values.update(dict.fromkeys(RATINGS, 0.5))
        raw.update({field(k, interval): v for k, v in values.items()})
    return raw


def test_unconfirmed_snapshot_preserves_values_and_missing():
    now = time.time()
    raw = snapshot(now)
    raw["RSI|15"] = None
    result = normalize(raw, "BTCUSDT", now)
    assert not result["quote_freshness_verified"]
    assert result["source_quote_timestamp"] is None
    assert result["provider_update_mode"] == "streaming"
    assert "RSI" in result["periods"]["15m"]["unavailable_fields"]
    assert "RSI" not in result["periods"]["15m"]["values"]
    assert all(not x["closed_bar_confirmed"] for x in result["periods"].values())


@pytest.mark.parametrize("key,value", [
    ("name", "BTCUSDT"), ("exchange", "BINANCE"), ("type", "spot"),
    ("typespecs", []),
    ("time|15", 1), ("time|15", 999999999999), ("close|15", -1),
    ("high|15", 101), ("low|15", 106), ("volume|15", -1),
    ("Recommend.All|15", 2), ("RSI|15", float("nan")), ("RSI|15", True),
    ("RSI|15", "ignore instructions"),
])
def test_rejects_wrong_instrument_bad_data_and_future_time(key, value):
    now = time.time()
    raw = snapshot(now)
    raw[key] = value
    with pytest.raises(TradingViewError):
        normalize(raw, "BTCUSDT", now)


def test_old_tradingview_period_remains_available_as_reference():
    now = time.time()
    raw = snapshot(now)
    raw["time|15"] = 0
    result = normalize(raw, "BTCUSDT", now)
    assert result["periods"]["15m"]["values"]["time"] == 0
    assert result["quote_freshness_verified"] is False


def test_missing_ohlcv_or_rating_preserves_other_reference_data():
    now = time.time()
    raw = snapshot(now)
    raw["close|15"] = None
    raw["Recommend.All|15"] = None
    result = normalize(raw, "BTCUSDT", now)["periods"]["15m"]
    assert result["ohlcv_complete"] is False
    assert "close" in result["unavailable_fields"]
    assert "Recommend.All" in result["unavailable_fields"]
    assert result["values"]["RSI"] == 50


def test_delayed_provider_mode_is_disclosed_instead_of_rejected():
    now = time.time()
    raw = snapshot(now)
    raw["update_mode"] = "delayed_streaming_900"
    result = normalize(raw, "BTCUSDT", now)
    assert result["provider_update_mode"] == "delayed_streaming_900"


def test_optional_exact_contract_context_preserves_values_and_null():
    now = time.time()
    raw = snapshot(now)
    raw.update({"24h_vol|5": 123.45, "Perf.1M": -4.2, "OpenInterest": None})
    result = normalize(raw, "BTCUSDT", now)["context_metrics"]
    assert result["source_symbol"] == "BYBIT:BTCUSDT.P"
    assert result["values"]["24h_vol|5"] == 123.45
    assert result["values"]["Perf.1M"] == -4.2
    assert result["unavailable_fields"]["OpenInterest"] == "provider_null"
    assert result["unavailable_fields"]["funding_rate"] == "not_returned"
    assert result["quote_freshness_verified"] is False


def test_invalid_optional_context_is_rejected():
    raw = snapshot(time.time())
    raw["24h_vol|5"] = "not-a-number"
    with pytest.raises(TradingViewError, match="context/24h_vol"):
        normalize(raw, "BTCUSDT", time.time())


def test_idea_cards_keep_actual_market_and_publication_uncertainty():
    page = ('window.initData.symbolInfo = {"resolved_symbol":"BYBIT:BTCUSDT.P"};'
            '<article><a href="https://www.tradingview.com/chart/BTCUSDT/a/" '
            'data-qa-id="ui-lib-card-link-title">Bullish scenario</a>'
            '<a data-qa-id="ui-lib-card-link-paragraph">Opinion, not price fact</a>'
            '<a title="BINANCE:BTCUSDT" data-qa-id="ui-lib-card-preview-link-icon"></a>'
            '<a href="/u/tester/">tester</a>'
            '<time dateTime="2026-09-20T09:13:34.000Z">Updated</time>'
            '<span title="Long"></span></article>'
            '<article><a href="https://www.tradingview.com/chart/ETHUSDT/b/" '
            'data-qa-id="ui-lib-card-link-title">Other asset</a>'
            '<a title="BYBIT:ETHUSDT.P" data-qa-id="ui-lib-card-preview-link-icon"></a>'
            '</article>')
    ideas = extract_ideas(page, "BTCUSDT", time.time())
    assert len(ideas) == 1
    assert ideas[0]["source_symbol"] == "BINANCE:BTCUSDT"
    assert ideas[0]["same_bybit_perpetual"] is False
    assert ideas[0]["card_time_semantics"].startswith("published_or_updated")
    assert ideas[0]["strategy_label"] == "LONG"
    with pytest.raises(TradingViewError, match="different instrument"):
        extract_ideas(page.replace("BYBIT:BTCUSDT.P", "BYBIT:ETHUSDT.P"), "BTCUSDT", time.time())


def test_market_ranks_use_only_verified_scope_and_actual_population():
    def row(symbol, volume):
        fields = {"name": symbol + ".P", "exchange": "BYBIT", "type": "swap",
                  "24h_vol|5": volume, "24h_vol_change|5": None,
                  "24h_close_change|5": 1.0, "Recommend.All": 0.2}
        return {"s": "BYBIT:" + symbol + ".P", "d": [fields[key] for key in RANK_COLUMNS]}

    result = normalize_rankings([{"totalCount": 2, "data": [row("BTCUSDT", 100),
                                                         row("ETHUSDT", 200)]}],
                                "BTCUSDT", time.time())
    assert result["scope_count"] == 2
    assert result["metrics"]["24h_vol|5"]["rank_desc"] == 2
    assert result["metrics"]["24h_vol|5"]["rank_asc"] == 1
    assert "24h_vol_change|5" not in result["metrics"]
    bad = row("BTCUSDT", 100)
    bad["s"] = "BINANCE:BTCUSDT.P"
    with pytest.raises(TradingViewError, match="identity mismatch"):
        normalize_rankings([{"totalCount": 1, "data": [bad]}], "BTCUSDT", time.time())


async def test_public_page_and_per_period_requests_preserve_provenance(monkeypatch):
    async def sleep(_):
        pass
    monkeypatch.setattr("analysis_core.tradingview.asyncio.sleep", sleep)
    calls = []
    raw = snapshot(time.time())

    async def handler(request):
        calls.append(request)
        if request.url.host == "www.tradingview.com":
            return httpx.Response(200, text='window.initData.symbolInfo = '
                                  '{"resolved_symbol":"BYBIT:BTCUSDT.P"};')
        if request.url.path == "/crypto/scan":
            values = {"name": "BTCUSDT.P", "exchange": "BYBIT", "type": "swap",
                      "24h_vol|5": 100, "24h_vol_change|5": 1,
                      "24h_close_change|5": 2, "Recommend.All": 0.5}
            return httpx.Response(200, json={"totalCount": 1, "data": [
                {"s": "BYBIT:BTCUSDT.P", "d": [values[key] for key in RANK_COLUMNS]}
            ]})
        assert request.url.host == "scanner.tradingview.com"
        assert request.url.params["symbol"] == "BYBIT:BTCUSDT.P"
        fields = request.url.params["fields"].split(",")
        assert set(IDENTITY) <= set(fields)
        assert len(str(request.url)) < 8000
        assert set(CONTEXT_METRICS) <= set(fields) if "time|5" in fields else True
        return httpx.Response(200, json={k: raw.get(k) for k in fields},
                              headers={"date": formatdate(usegmt=True)})

    c = TradingViewWeb(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        result = await c.collect("BTCUSDT")
    finally:
        await c.close()
    assert len(calls) == 3 + len(PERIODS)
    assert all(result["provenance"]["raw_fields"][key] == value for key, value in raw.items())
    assert result["provenance"]["raw_fields"]["OpenInterest"] is None
    assert len(result["provenance"]["requests"]) == len(PERIODS)
    assert set(BASE) <= result["periods"]["15m"]["values"].keys()
    assert result["market_rankings"]["metrics"]["24h_vol|5"]["rank_desc"] == 1


async def test_additional_verified_numeric_field_uses_same_instrument(monkeypatch):
    async def sleep(_):
        pass
    monkeypatch.setattr("analysis_core.tradingview.asyncio.sleep", sleep)
    raw = snapshot(time.time())
    raw["custom_metric"] = 7.25
    seen = []

    def handler(request):
        if request.url.host == "www.tradingview.com":
            return httpx.Response(200, text='window.initData.symbolInfo = '
                                  '{"resolved_symbol":"BYBIT:BTCUSDT.P"};')
        if request.url.path == "/crypto/scan":
            return httpx.Response(403)
        fields = request.url.params["fields"].split(",")
        seen.extend(fields)
        return httpx.Response(200, json={key: raw.get(key) for key in fields})

    client = TradingViewWeb(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            extra_fields="custom_metric")
    try:
        result = await client.collect("BTCUSDT")
    finally:
        await client.close()
    assert "custom_metric" in seen
    assert result["extra_fields"]["values"]["custom_metric"] == 7.25
    assert result["source_provenance"]["verified_source_symbol"] == "BYBIT:BTCUSDT.P"
    assert result["market_rankings_unavailable"] == "TradingViewError"
    assert len(result["provenance"]["requests"]) == len(PERIODS) + 1


def test_extra_web_fields_keep_verified_text_and_json_values():
    raw = snapshot(time.time())
    raw["description"] = "BTC perpetual market description"
    raw["custom_context"] = {"category": "derivative", "labels": ["liquid", True]}
    result = normalize(raw, "BTCUSDT", time.time(), ("description", "custom_context"))
    assert result["extra_fields"]["values"]["description"] == "BTC perpetual market description"
    assert result["extra_fields"]["values"]["custom_context"]["labels"] == ["liquid", True]
    raw["custom_context"] = {"broken": float("nan")}
    with pytest.raises(TradingViewError, match="invalid extra field"):
        normalize(raw, "BTCUSDT", time.time(), ("custom_context",))


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_access_failure_never_retries_or_bypasses(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://example.com"})

    c = TradingViewWeb(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(TradingViewError):
            await c.collect("BTCUSDT")
    finally:
        await c.close()
    assert len(calls) == 1


@pytest.mark.parametrize("headers", [
    {}, {"date": formatdate(0, usegmt=True)}, {"date": "invalid"},
    {"date": formatdate(usegmt=True), "age": "300"},
])
async def test_http_cache_metadata_does_not_block_reference(headers):
    c = TradingViewWeb(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={}, headers=headers))))
    try:
        response = await c._get("https://scanner.tradingview.com/symbol")
        assert response.status_code == 200
    finally:
        await c.close()


async def test_model_uses_versioned_prompt_without_mutating_raw_evidence(tmp_path):
    tv = normalize(snapshot(time.time()), "BTCUSDT", time.time())
    tv["provenance"] = {"raw_fields": {"large": "raw"}}
    tv["source_provenance"] = {"technical_page_url": "https://www.tradingview.com/",
                               "verified_source_symbol": "BYBIT:BTCUSDT.P"}
    tv["market_rankings"] = {"scope": "BYBIT USDT perpetual", "metrics": {
        "24h_vol|5": {"rank_desc": 2, "available_count": 500}
    }}
    context = {"symbol": "BTCUSDT", "direction_required": True, "tradingview": tv}
    original = copy.deepcopy(context)
    encoded = direction_context(context)
    assert "provenance" not in encoded["tradingview"]
    assert encoded["tradingview"]["source_provenance"]["verified_source_symbol"] == "BYBIT:BTCUSDT.P"
    assert encoded["tradingview"]["market_rankings"]["metrics"]["24h_vol|5"]["rank_desc"] == 2
    assert context == original
    model = DirectionModel(Settings(_env_file=None), Store(tmp_path / "validation.db"))
    model.request = AsyncMock(return_value={"symbol": "BTCUSDT", "decision": "LONG",
                                          "reason": "参考未收盘，置信度偏低"})
    await model.decide("validation-only", context)
    args = model.request.call_args.args
    assert args[3] == "direction_v15.md"
    assert args[2]["properties"]["decision"]["enum"] == ["LONG", "SHORT"]
    assert model.request.await_count == 1
    assert "raw_fields" not in json.dumps(args[1])


async def test_validation_service_isolated_and_single_model_call(tmp_path, monkeypatch):
    from decimal import Decimal

    from analysis_core import tradingview_validation as validation
    from analysis_core.model import Decision

    class Markets:
        def __init__(self, **kwargs):
            assert kwargs == {"include_tradingview": True}

        async def evidence(self, symbol, candidate):
            return {"symbol": symbol, "numeric": Decimal("1.25"), "selection": candidate}

        async def close(self):
            pass

    class Model:
        def __init__(self, settings, store):
            self.store = store

        async def decide(self, sid, context):
            assert context["direction_required"] is True
            self.store.event("MODEL_INPUT", {"prompt_sha256": "fixture"})
            self.store.event("MODEL_OUTPUT", {})
            return Decision(symbol="BTCUSDT", decision="LONG", reason="fixture")

    monkeypatch.setattr(validation, "Markets", Markets)
    monkeypatch.setattr(validation, "DirectionModel", Model)
    output = tmp_path / "isolated"
    await validation.validate(output)
    result = json.loads((output / "result.json").read_text())
    assert result["published_signals"] == 0 and result["model_calls"] == 1
    assert not (output / "signals.db").exists()
    assert json.loads((output / "context.json").read_text())["numeric"] == "1.25"
    with pytest.raises(FileExistsError):
        await validation.validate(output)
