import time
from types import SimpleNamespace

import pytest

from analysis_core.tradingview import RANK_COLUMNS, TradingViewError
from analysis_core.tradingview_discovery import TradingViewDiscovery
from analysis_core.tradingview_sources import CoinContextSource, SourceRegistry


def cex_row(symbol, *, volume=100, change=1, rating=0.2):
    values = {"name": symbol + ".P", "exchange": "BYBIT", "type": "swap",
              "24h_vol|5": volume, "24h_vol_change|5": change,
              "24h_close_change|5": change, "Recommend.All": rating}
    return {"s": "BYBIT:" + symbol + ".P", "d": [values[k] for k in RANK_COLUMNS]}


class Web:
    def __init__(self, page):
        self.page = page
        self.calls = []

    async def _post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: self.page)


async def test_tv_discovery_only_returns_verified_tradable_contracts():
    web = Web({"totalCount": 3, "data": [
        cex_row("AUSDT", volume=1000),
        cex_row("BUSDT", volume=500, change=-5, rating=-0.5),
        cex_row("NOTTRADEUSDT", volume=3000),
    ]})
    rows = await TradingViewDiscovery(web).collect({"AUSDT", "BUSDT"})
    assert {row["symbol"] for row in rows} == {"AUSDT", "BUSDT"}
    assert all(row["market_rankings"]["target_present"] for row in rows)
    assert all(row["source_symbol"] == f"BYBIT:{row['symbol']}.P" for row in rows)
    assert len(web.calls) == 1
    assert web.calls[0][1]["json"]["filter"][0]["right"] == "BYBIT"


async def test_tv_discovery_fails_closed_on_wrong_contract_identity():
    row = cex_row("AUSDT")
    row["s"] = "BINANCE:AUSDT.P"
    with pytest.raises(TradingViewError):
        await TradingViewDiscovery(Web({"totalCount": 1, "data": [row]})).collect({"AUSDT"})


async def test_discovery_pool_includes_liquid_major_and_both_price_tails():
    rows = [
        cex_row(f"C{index}USDT", volume=1000 - index,
                change=index - 15, rating=0.1)
        for index in range(30)
    ]
    rows.append(cex_row("MAJORUSDT", volume=100000, change=0, rating=0))
    selected = await TradingViewDiscovery(
        Web({"totalCount": len(rows), "data": rows}), pool_limit=20
    ).collect({f"C{index}USDT" for index in range(30)} | {"MAJORUSDT"})
    symbols = {row["symbol"] for row in selected}
    assert "MAJORUSDT" in symbols
    assert "C0USDT" in symbols and "C29USDT" in symbols
    assert len(selected) == 20


async def test_coin_context_is_weak_aggregated_background():
    class CoinWeb:
        async def _post(self, url, **kwargs):
            body = kwargs["json"]
            values = ["BTCUSD", 1000, 2, None, None, None, 73, None, 3]
            assert body["filter"][0]["right"] == "BTCUSD"
            return SimpleNamespace(json=lambda: {
                "totalCount": 1, "data": [{"s": "CRYPTO:BTCUSD", "d": values}]
            })

    result = await CoinContextSource(CoinWeb()).collect("BTCUSDT")
    assert result["scope"] == "aggregated_coin_not_bybit_contract"
    assert result["values"]["sentiment"] == 73
    assert "social_volume" in result["unavailable_fields"]


async def test_optional_source_failure_is_reported_without_fabrication():
    class Failing:
        name = "news"
        stage = "final"

        async def collect(self, symbol):
            raise TradingViewError("unavailable")

    result = await SourceRegistry((Failing(),)).enrich("BTCUSDT", {"fetched_at": time.time()}, stage="final")
    assert "news" not in result and "news_unavailable" in result
