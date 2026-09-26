from datetime import UTC, datetime

import pytest

from analysis_core.config import Settings
from analysis_core.model import ModelServiceError
from analysis_core.selection import (
    FinalSelection,
    SelectionModels,
    TVSelection,
    compact_bybit,
    compact_tradingview,
)
from analysis_core.store import Store


class Runner:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def request(self, *args):
        self.calls.append(args)
        return self.answer


def tv(symbol):
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "source_provenance": {"verified_source_symbol": f"BYBIT:{symbol}.P"},
        "periods": {"1h": {"values": {"close": 100, "RSI": 55, "unrelated": 99},
                            "unavailable_fields": ["EMA20"], "closed_bar_confirmed": False}},
        "coin_context": {"scope": "aggregated_coin_not_bybit_contract"},
    }


def bybit(symbol):
    return {
        "symbol": symbol, "observed_at": datetime.now(UTC).isoformat(),
        "candles": {"1h": [{"completed": True, "close": n} for n in range(15)]},
        "indicators": {"1h": {"recent_windows": []}},
        "price_volume": {},
        "orderflow": {"book_snapshots": ["large"], "trade_sample": ["large"],
                      "sample_windows": []},
    }


def tv_answer(symbols):
    return {"candidates": [
        {"symbol": symbol, "rank": n, "direction": "LONG", "confidence": "LOW",
         "reason": "initial TradingView evidence"}
        for n, symbol in enumerate(symbols, 1)
    ]}


def final_answer(symbols):
    return {"selected": [
        {"symbol": symbol, "rank": n, "direction": "SHORT", "confidence": "LOW",
         "reason": "Bybit data changed the direction"}
        for n, symbol in enumerate(symbols, 1)
    ]}


async def test_tradingview_initial_selection_and_bybit_final_ranking(tmp_path):
    a, b = "AUSDT", "BUSDT"
    tv_runner = Runner(tv_answer([a, b]))
    final_runner = Runner(final_answer([b]))
    models = SelectionModels(Settings(_env_file=None), Store(tmp_path / "db"),
                             tv_runner=tv_runner, final_runner=final_runner)
    initial = await models.choose_tv("cycle", [
        {"symbol": symbol, "discovery": {"rank": n}, "tradingview": tv(symbol)}
        for n, symbol in enumerate((a, b), 1)
    ])
    assert [x.symbol for x in initial] == [a, b]
    final = await models.choose_final("cycle", [
        {"symbol": symbol, "tv_initial": item.model_dump(),
         "tradingview": tv(symbol), "bybit": bybit(symbol)}
        for symbol, item in zip((a, b), initial, strict=True)
    ])
    assert [(x.symbol, x.direction) for x in final] == [(b, "SHORT")]
    assert len(tv_runner.calls) == len(final_runner.calls) == 1
    assert "unrelated" not in str(tv_runner.calls[0][1])
    assert "book_snapshots" not in str(final_runner.calls[0][1])
    assert models.store.rows("SELECT * FROM events WHERE kind='FINAL_SELECTION_RESULT'")


@pytest.mark.parametrize("answer", [
    {"candidates": []},
    tv_answer(["OUTSIDEUSDT"]),
    {"candidates": [
        {"symbol": "AUSDT", "rank": 2, "direction": "LONG", "confidence": "LOW",
         "reason": "wrong rank"}
    ]},
])
async def test_tv_stage_rejects_empty_unknown_or_discontinuous(tmp_path, answer):
    models = SelectionModels(Settings(_env_file=None), Store(tmp_path / "db"),
                             tv_runner=Runner(answer))
    with pytest.raises(ModelServiceError):
        await models.choose_tv("cycle", [{"symbol": "AUSDT", "discovery": {},
                                           "tradingview": tv("AUSDT")}])


@pytest.mark.parametrize("answer", [
    {"selected": []},
    final_answer(["OUTSIDEUSDT"]),
    final_answer(["AUSDT", "AUSDT"]),
])
async def test_final_stage_rejects_empty_unknown_or_duplicate(tmp_path, answer):
    models = SelectionModels(Settings(_env_file=None), Store(tmp_path / "db"),
                             final_runner=Runner(answer))
    with pytest.raises(ModelServiceError):
        await models.choose_final("cycle", [{"symbol": "AUSDT", "tv_initial": {},
                                             "tradingview": tv("AUSDT"),
                                             "bybit": bybit("AUSDT")}])


def test_compaction_keeps_1h_evidence_and_source_scope():
    assert compact_tradingview(tv("AUSDT"))["coin_context"]["scope"].startswith("aggregated")
    compact = compact_bybit(bybit("AUSDT"))
    assert len(compact["candles"]["1h"]) == 8
    assert "book_snapshots" not in compact["orderflow"]
    with pytest.raises(ValueError):
        TVSelection.model_validate({"candidates": []})
    with pytest.raises(ValueError):
        FinalSelection.model_validate({"selected": []})


@pytest.mark.parametrize("stage", ["tv", "final"])
async def test_model_request_failure_stops_after_one_call(tmp_path, stage):
    from unittest.mock import AsyncMock

    runner = AsyncMock()
    runner.request.side_effect = ValueError("invalid model JSON")
    models = SelectionModels(Settings(_env_file=None), Store(tmp_path / "db"),
                             tv_runner=runner, final_runner=runner)
    bundle = {"symbol": "AUSDT", "discovery": {}, "tv_initial": {},
              "tradingview": tv("AUSDT"), "bybit": bybit("AUSDT")}
    with pytest.raises(ModelServiceError):
        await getattr(models, "choose_" + stage)("cycle", [bundle])
    runner.request.assert_awaited_once()
    runner.reset_mock()
    with pytest.raises(ValueError):
        await getattr(models, "choose_" + stage)("empty", [])
    runner.request.assert_not_awaited()
