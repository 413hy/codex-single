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


async def test_old_bybit_evidence_cannot_be_repackaged_as_new_signal(producer):
    original = producer.markets.evidence.side_effect

    async def stale(symbol, candidate):
        result = await original(symbol, candidate)
        result["observed_at"] = datetime.fromtimestamp(time.time() - 601, UTC).isoformat()
        return result

    producer.markets.evidence.side_effect = stale
    assert not await producer.cycle("expired-evidence")
    assert publications(producer) == []
    assert producer.store.rows("SELECT * FROM incidents WHERE scope='PRIMARY_DIRECTION'")


@pytest.mark.parametrize("initial_age,model_elapsed,expected", [(0, 601, False), (590, 11, False), (0, 300, True)])
async def test_hedge_evidence_is_checked_again_after_model(
    producer, monkeypatch, initial_age, model_elapsed, expected,
):
    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    original_evidence = producer.markets.evidence.side_effect
    original_decide = producer.model.decide.side_effect

    async def evidence(symbol, candidate):
        result = await original_evidence(symbol, candidate)
        result["observed_at"] = datetime.fromtimestamp(clock[0] - initial_age, UTC).isoformat()
        return result

    async def delayed_decide(sid, context):
        clock[0] += model_elapsed
        return original_decide(sid, context)

    producer.markets.evidence.side_effect = evidence
    producer.model.decide.side_effect = delayed_decide
    assert await producer.cycle("hedge-model-delay") is expected
    producer.model.decide.assert_awaited_once()
    rows = publications(producer)
    assert {r["symbol"] for r in rows if r["normal_candidate"]} == {"AUSDT", "BUSDT"}
    assert any(r["symbol"] == "HEDGEUSDT" for r in rows) is expected
    if not expected:
        assert producer.store.rows(
            "SELECT status FROM signals WHERE symbol='HEDGEUSDT'"
        )[0]["status"] == "ERROR"
        assert producer.store.rows(
            "SELECT * FROM incidents WHERE scope='DIRECTION:HEDGEUSDT' AND status='OPEN'"
        )


async def test_one_tradingview_page_failure_is_audited_without_stopping_other_symbols(producer):
    original = producer.markets.tradingview_evidence.side_effect

    async def one_bad_page(candidate):
        if candidate["symbol"] == "BUSDT":
            raise ValueError("TradingView technical page HTTP 404")
        return await original(candidate)

    producer.markets.tradingview_evidence.side_effect = one_bad_page
    producer.selection.choose_tv.return_value = [
        TVCandidate(symbol="AUSDT", rank=1, direction="LONG", confidence="LOW",
                    reason="TradingView preliminary direction")
    ]
    producer.selection.choose_final.return_value = [
        FinalCandidate(symbol="AUSDT", rank=1, direction="LONG", confidence="LOW",
                       reason="Bybit and TradingView support a cautious direction")
    ]
    assert not await producer.cycle("one-bad-page")
    assert {row["symbol"] for row in publications(producer)} == {"AUSDT", "HEDGEUSDT"}
    assert producer.store.rows(
        "SELECT * FROM incidents WHERE scope='TV_EVIDENCE:BUSDT' AND status='OPEN'"
    )
    assert producer.store.rows(
        "SELECT * FROM events WHERE kind='TV_EVIDENCE_FAILURE'"
    )


async def test_discovery_ineligibility_does_not_claim_source_recovery(producer):
    producer.store.incident("TV_EVIDENCE:MSFUUSDT", "analysis", {}, "stale core bar")
    producer.markets.discovery_exclusions = [{
        "symbol": "MSFUUSDT", "reason": "stale_or_missing_tradingview_core_bars",
        "fields": ["time|15"],
    }]
    assert await producer.cycle("fresh-pool")
    assert producer.store.rows(
        "SELECT status FROM incidents WHERE scope='TV_EVIDENCE:MSFUUSDT'"
    )[0]["status"] == "OPEN"
    event = producer.store.rows("SELECT payload FROM events WHERE kind='TV_DISCOVERY'")[0]
    assert json.loads(event["payload"])["excluded"][0]["symbol"] == "MSFUUSDT"


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
    producer.selection.choose_final.side_effect = ModelServiceError("outside evidence pool")
    assert not await producer.cycle("bad-final")
    assert publications(producer) == []
    producer.model.decide.assert_not_awaited()
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


@pytest.mark.parametrize("invalid", [
    None, "unowned", "wrong_symbol", "duplicate_group", "same_side",
    "stale_positions", "missing_observation", "future_observation",
])
def test_sniffer_reads_only_fresh_owned_locked_pairs(tmp_path, invalid):
    store = Store(tmp_path / "hedge.db")
    store.execute("CREATE TABLE trades (trade_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, "
                  "position_idx INTEGER, status TEXT, owned INTEGER, qty TEXT, entry_price TEXT)")
    store.execute("CREATE TABLE hedge_groups (group_id TEXT PRIMARY KEY, document TEXT)")
    now = time.time()
    store.set("monitor_heartbeat", now)
    store.set("exchange_positions_observed_at", now)
    store.set("exchange_positions", [
        {"symbol": "TESTUSDT", "side": side, "positionIdx": idx,
         "size": "1", "avgPrice": "100"}
        for idx, side in ((1, "Buy"), (2, "Sell"))
    ])
    for tid, side, idx in (("a", "LONG", 1), ("b", "SHORT", 2)):
        store.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)", (
            tid, "TESTUSDT", side, idx, "OPEN", 1, "1", "100"))
    store.execute("INSERT INTO hedge_groups VALUES (?,?)", (
        "g", json.dumps({"group_id": "g", "symbol": "TESTUSDT", "generation": 2,
                         "active": "a", "child": "b", "phase": "LOCKED"}),
    ))
    if invalid == "stale_positions":
        store.set("exchange_positions_observed_at", now - 21)
    elif invalid == "missing_observation":
        store.execute("DELETE FROM state WHERE key='exchange_positions_observed_at'")
    elif invalid == "future_observation":
        store.set("exchange_positions_observed_at", now + 1)
    elif invalid == "unowned":
        store.execute("UPDATE trades SET owned=0 WHERE trade_id='a'")
    elif invalid == "wrong_symbol":
        store.execute("UPDATE hedge_groups SET document=json_set(document,'$.symbol','OTHERUSDT')")
    elif invalid == "duplicate_group":
        store.execute("INSERT INTO hedge_groups VALUES (?,?)", (
            "g2", json.dumps({"group_id": "g2", "symbol": "TESTUSDT", "generation": 1,
                              "active": "a", "child": "b", "phase": "LOCKED"}),
        ))
    elif invalid == "same_side":
        store.execute("UPDATE trades SET side='LONG' WHERE trade_id='b'")
        store.set("exchange_positions", [
            {"symbol": "TESTUSDT", "side": "Buy", "positionIdx": idx,
             "size": "1", "avgPrice": "100"} for idx in (1, 2)
        ])
    sniffer = HedgeSniffer(store.path)
    if invalid:
        with pytest.raises(RuntimeError):
            sniffer.sniff(now)
    else:
        assert sniffer.sniff(now)["TESTUSDT"]["generation"] == 2
        with pytest.raises(RuntimeError):
            sniffer.sniff(now + 21)
