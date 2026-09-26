import json
import sqlite3
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from analysis_core.app import AnalysisApp
from analysis_core.config import Settings
from analysis_core.model import Decision, ModelServiceError
from analysis_core.selection import FinalCandidate, TVCandidate
from analysis_core.signals import HedgeSniffer
from analysis_core.store import Store


class Sniffer:
    def __init__(self, symbols=None):
        self.symbols = symbols or {"AUSDT", "HEDGEUSDT"}

    def sniff(self):
        return {
            symbol: {"group_id": symbol + ":group", "generation": 3, "positions": []}
            for symbol in self.symbols
        }


@pytest.fixture
def producer(tmp_path):
    markets = AsyncMock()
    markets.discover.return_value = [
        {"symbol": symbol, "market_rankings": {"source_symbol": f"BYBIT:{symbol}.P"}}
        for symbol in ("AUSDT", "BUSDT", "HEDGEUSDT")
    ]

    async def tv_evidence(candidate):
        symbol = candidate["symbol"]
        return {"fetched_at": datetime.now(UTC).isoformat(),
                "source_provenance": {"verified_source_symbol": f"BYBIT:{symbol}.P"}}

    async def evidence(symbol, candidate):
        return {
            "symbol": symbol,
            "observed_at": datetime.now(UTC).isoformat(),
            "tradingview": candidate.get("tradingview") or await tv_evidence({"symbol": symbol}),
        }

    markets.tradingview_evidence.side_effect = tv_evidence
    markets.evidence.side_effect = evidence
    selection = AsyncMock()
    selection.choose_tv.return_value = [
        TVCandidate(symbol=symbol, rank=rank, direction="LONG", confidence="LOW",
                    reason="TradingView preliminary direction")
        for rank, symbol in enumerate(("AUSDT", "BUSDT", "HEDGEUSDT"), 1)
    ]
    selection.choose_final.return_value = [
        FinalCandidate(symbol=symbol, rank=rank, direction="LONG", confidence="LOW",
                       reason="Bybit and TradingView support a cautious direction")
        for rank, symbol in enumerate(("AUSDT", "BUSDT"), 1)
    ]
    model = AsyncMock()
    model.decide.side_effect = lambda sid, context: Decision(
        symbol=context["symbol"], decision="SKIP", reason="Evidence conflicts"
    )
    return AnalysisApp(
        Settings(_env_file=None, runtime_dir=tmp_path),
        markets=markets, model=model, selection=selection, sniffer=Sniffer(),
    )


def publications(app):
    with sqlite3.connect(app.bus.path) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT payload FROM publications ORDER BY id")]


async def test_two_stage_selection_hedge_dedup_and_ten_minute_signals(producer):
    assert await producer.cycle("one")
    producer.selection.choose_tv.assert_awaited_once()
    producer.selection.choose_final.assert_awaited_once()
    assert producer.model.decide.await_count == 1
    assert [r["symbol"] for r in publications(producer)] == ["AUSDT", "BUSDT", "HEDGEUSDT"]
    rows = publications(producer)
    assert rows[0]["normal_candidate"] is True and rows[0]["hedge"]["generation"] == 3
    assert rows[1]["normal_candidate"] is True and rows[1]["hedge"] is None
    assert rows[2]["normal_candidate"] is False and rows[2]["decision"] == "SKIP"
    assert all(r["version"] == 2 and r["expires_at"] - r["published_at"] == 600 for r in rows)
    assert [c.args[0] for c in producer.markets.evidence.call_args_list] == [
        "AUSDT", "BUSDT", "HEDGEUSDT"
    ]
    # The overlap already has a final direction, so no extra hedge model call occurs.
    assert producer.model.decide.call_args.args[1]["symbol"] == "HEDGEUSDT"
    assert await producer.cycle("one")
    assert producer.selection.choose_tv.await_count == 1


async def test_snapshot_failure_blocks_publication_after_normal_refinement(producer):
    def stale():
        raise RuntimeError("stale snapshot")

    producer.sniffer.sniff = stale
    assert not await producer.cycle("stale")
    producer.selection.choose_final.assert_awaited_once()
    assert publications(producer) == []
    assert producer.store.rows("SELECT status FROM cycles WHERE cycle_id='stale'")[0]["status"] == "PARTIAL_ERROR"


async def test_normal_data_failure_still_analyzes_locked_hedge(producer):
    producer.markets.discover.side_effect = ValueError("TV unavailable")
    assert not await producer.cycle("tv-error")
    assert producer.model.decide.await_count == 2
    assert {r["symbol"] for r in publications(producer)} == {"AUSDT", "HEDGEUSDT"}
    assert all(not r["normal_candidate"] for r in publications(producer))


async def test_model_service_failure_stops_remaining_model_calls(producer):
    producer.selection.choose_tv.side_effect = ModelServiceError("quota")
    assert not await producer.cycle("quota")
    producer.model.decide.assert_not_awaited()
    assert publications(producer) == []


async def test_final_model_cannot_publish_unknown_or_missing_primary(producer):
    producer.selection.choose_final.side_effect = ValueError("outside evidence pool")
    assert not await producer.cycle("bad-final")
    assert all(not r["normal_candidate"] for r in publications(producer))
    assert producer.store.rows("SELECT * FROM incidents WHERE scope='PRIMARY_DIRECTION' AND status='OPEN'")


async def test_cancelled_cycle_is_interrupted_and_not_replayed(producer):
    import asyncio

    producer.selection.choose_final.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await producer.cycle("cancelled")
    row = producer.store.rows("SELECT status FROM cycles WHERE cycle_id='cancelled'")[0]
    assert row["status"] == "INTERRUPTED"
    await producer.cycle("cancelled")
    assert producer.selection.choose_final.await_count == 1


def test_duplicate_publication_does_not_extend_validity(producer):
    decision = Decision(symbol="TESTUSDT", decision="LONG", reason="structure")
    one = producer.bus.publish(
        "same", decision, cycle_id="cycle", analysis_started_at=90,
        normal=True, hedge=None, observed_at="now", now=100,
    )
    two = producer.bus.publish(
        "same", decision, cycle_id="cycle", analysis_started_at=90,
        normal=True, hedge=None, observed_at="now", now=200,
    )
    assert one == two and two["expires_at"] == 700


async def test_cycle_has_one_notification_and_scheduled_slot_is_not_replayed(producer):
    assert await producer.cycle("merged")
    assert len(producer.store.rows("SELECT * FROM outbox WHERE event_key='cycle:merged'")) == 1
    producer.store.set("next_analysis_at", time.time() - 3600)
    await producer.cycle()
    assert producer.selection.choose_tv.await_count == 1


@pytest.mark.parametrize("invalid", [None, "unowned", "wrong_symbol"])
def test_sniffer_reads_only_fresh_owned_locked_pairs(tmp_path, invalid):
    store = Store(tmp_path / "hedge.db")
    store.execute("DROP INDEX owned_active_slot")
    now = time.time()
    store.set("monitor_heartbeat", now)
    store.set("exchange_positions", [
        {"symbol": "TESTUSDT", "side": side, "positionIdx": idx,
         "size": "1", "avgPrice": "100"}
        for idx, side in ((1, "Buy"), (2, "Sell"))
    ])
    for tid, side, idx in (("a", "LONG", 1), ("b", "SHORT", 2)):
        store.insert_trade({
            "trade_id": tid, "symbol": "TESTUSDT", "side": side,
            "position_idx": idx, "status": "OPEN", "qty": "1",
            "entry_price": "100", "details": "{}",
        })
    store.execute("INSERT INTO hedge_groups VALUES (?,?)", (
        "g", json.dumps({"group_id": "g", "symbol": "TESTUSDT", "generation": 2,
                         "active": "a", "child": "b", "phase": "LOCKED"}),
    ))
    if invalid == "unowned":
        store.execute("UPDATE trades SET owned=0 WHERE trade_id='a'")
    elif invalid == "wrong_symbol":
        store.execute("UPDATE hedge_groups SET document=json_set(document,'$.symbol','OTHERUSDT')")
    sniffer = HedgeSniffer(store.path)
    if invalid:
        with pytest.raises(RuntimeError):
            sniffer.sniff(now)
    else:
        assert sniffer.sniff(now)["TESTUSDT"]["generation"] == 2
        with pytest.raises(RuntimeError):
            sniffer.sniff(now + 21)
