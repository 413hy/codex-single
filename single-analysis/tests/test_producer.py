import json
import sqlite3
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analysis_core.app import AnalysisApp
from analysis_core.config import Settings
from analysis_core.model import Decision, ModelServiceError
from analysis_core.signals import HedgeSniffer
from analysis_core.store import Store


class Sniffer:
    def sniff(self):
        return {"HEDGEUSDT": dict(group_id="group", generation=3, positions=[])}


@pytest.fixture
def producer(tmp_path):
    markets = AsyncMock()
    markets.scan.return_value = SimpleNamespace(
        candidates=[], failures={}, model_dump=lambda **k: {}
    )

    async def evidence(symbol, candidate):
        return dict(symbol=symbol, observed_at=datetime.now(UTC).isoformat(), selection=candidate)

    markets.evidence.side_effect = evidence
    model = AsyncMock()

    async def decide(sid, context):
        return Decision(
            symbol=context["symbol"],
            decision="LONG" if context.get("direction_required") else "SKIP",
            reason="relative trend or no direction",
        )

    model.decide.side_effect = decide
    screening = AsyncMock()
    screening.select.return_value = [{"symbol": "HEDGEUSDT"}, {"symbol": "OTHERUSDT"}]
    app = AnalysisApp(
        Settings(_env_file=None, runtime_dir=tmp_path),
        markets=markets,
        model=model,
        screening=screening,
        sniffer=Sniffer(),
    )
    return app


def publications(app):
    with sqlite3.connect(app.bus.path) as db:
        return [
            json.loads(r[0]) for r in db.execute("SELECT payload FROM publications ORDER BY id")
        ]


async def test_overlap_priority_single_call_skip_no_retry(producer):
    assert await producer.cycle("one")
    assert producer.model.decide.await_count == 2
    contexts = [c.args[1] for c in producer.model.decide.call_args_list]
    assert contexts[0]["symbol"] == "HEDGEUSDT" and "priority_review" in contexts[0]
    rows = publications(producer)
    assert rows[0]["normal_candidate"] is True and rows[0]["hedge"]["generation"] == 3
    assert all(r["expires_at"] - r["published_at"] == 60 for r in rows)
    assert await producer.cycle("one")
    assert producer.model.decide.await_count == 2


async def test_hedge_extra_outside_three_slots(producer):
    producer.screening.select.return_value = [{"symbol": s} for s in ["AUSDT", "BUSDT", "CUSDT"]]
    assert await producer.cycle("one")
    rows = publications(producer)
    assert len(rows) == 4 and sum(r["normal_candidate"] for r in rows) == 3
    assert rows[0]["symbol"] == "HEDGEUSDT" and not rows[0]["normal_candidate"]


async def test_screening_failure_still_analyzes_locked_coin(producer):
    producer.screening.select.side_effect = ValueError("bad screening citations")
    assert not await producer.cycle("one")
    assert producer.model.decide.await_count == 1
    assert publications(producer)[0]["symbol"] == "HEDGEUSDT"


async def test_service_outage_stops_all_additional_calls_and_recovers(producer):
    producer.screening.select.side_effect = ModelServiceError("quota")
    assert not await producer.cycle("one")
    producer.model.decide.assert_not_awaited()
    assert not publications(producer)
    producer.screening.select.side_effect = None
    producer.screening.select.return_value = [{"symbol": "NORMALUSDT"}]
    assert await producer.cycle("two")
    assert not producer.store.rows(
        "SELECT * FROM incidents WHERE scope='MODEL_SERVICE' AND status='OPEN'"
    )


async def test_stale_direction_never_published(producer):
    async def evidence(symbol, candidate):
        return dict(symbol=symbol, observed_at="2000-01-01T00:00:00+00:00")

    producer.markets.evidence.side_effect = evidence
    assert not await producer.cycle("one")
    assert publications(producer) == []


async def test_sniffer_failure_does_not_silently_omit_pairs(producer):
    def failed():
        raise RuntimeError("stale")

    producer.sniffer.sniff = failed
    assert not await producer.cycle("one")
    producer.markets.scan.assert_not_awaited()
    producer.model.decide.assert_not_awaited()


@pytest.mark.parametrize("invalid", [None, "unowned", "wrong_symbol"])
def test_sniffer_reads_only_fresh_owned_locked_pairs(tmp_path, invalid):
    store = Store(tmp_path / "hedge.db")
    store.execute("DROP INDEX owned_active_slot")
    now = time.time()
    store.set("monitor_heartbeat", now)
    store.set(
        "exchange_positions",
        [
            dict(symbol="TESTUSDT", side=side, positionIdx=idx, size="1", avgPrice="100")
            for idx, side in [(1, "Buy"), (2, "Sell")]
        ],
    )
    for tid, side, idx in [("a", "LONG", 1), ("b", "SHORT", 2)]:
        store.insert_trade(
            dict(
                trade_id=tid,
                symbol="TESTUSDT",
                side=side,
                position_idx=idx,
                status="OPEN",
                qty="1",
                entry_price="100",
                details="{}",
            )
        )
    store.execute(
        "INSERT INTO hedge_groups VALUES (?,?)",
        (
            "g",
            json.dumps(
                dict(
                    group_id="g",
                    symbol="TESTUSDT",
                    generation=2,
                    active="a",
                    child="b",
                    phase="LOCKED",
                )
            ),
        ),
    )
    if invalid == "unowned":
        store.execute("UPDATE trades SET owned=0 WHERE trade_id='a'")
    elif invalid == "wrong_symbol":
        store.execute("UPDATE hedge_groups SET document=json_set(document,'$.symbol','OTHERUSDT')")
    sniffer = HedgeSniffer(store.path)
    if invalid:
        with pytest.raises(RuntimeError):
            sniffer.sniff(now)
        return
    assert sniffer.sniff(now)["TESTUSDT"]["generation"] == 2
    with pytest.raises(RuntimeError):
        sniffer.sniff(now + 21)


async def test_cancelled_cycle_is_interrupted_and_not_replayed(producer):
    import asyncio

    producer.model.decide.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await producer.cycle("cancelled")
    assert (
        producer.store.rows("SELECT status FROM cycles WHERE cycle_id='cancelled'")[0]["status"]
        == "INTERRUPTED"
    )
    count = producer.model.decide.await_count
    await producer.cycle("cancelled")
    assert producer.model.decide.await_count == count


def test_duplicate_publication_does_not_extend_validity(producer):
    d = Decision(symbol="TESTUSDT", decision="LONG", reason="structure")
    one = producer.bus.publish("same", d, normal=True, hedge=None, observed_at="now", now=100)
    two = producer.bus.publish("same", d, normal=True, hedge=None, observed_at="now", now=200)
    assert one == two and two["expires_at"] == 160


async def test_primary_overlap_is_one_call_and_extra_skip_is_allowed(producer):
    producer.screening.select.return_value = [{"symbol": "AUSDT"}, {"symbol": "BUSDT"}]
    assert await producer.cycle("required")
    contexts = [c.args[1] for c in producer.model.decide.call_args_list]
    assert sum(c["direction_required"] for c in contexts) == 1
    assert next(c for c in contexts if c["direction_required"])["symbol"] == "AUSDT"
    rows = publications(producer)
    assert next(r for r in rows if r["symbol"] == "AUSDT")["decision"] == "LONG"
    assert next(r for r in rows if r["symbol"] == "HEDGEUSDT")["decision"] == "SKIP"


async def test_all_skip_is_failure_not_success_and_no_second_call(producer):
    async def abstain(sid, context):
        return Decision(symbol=context["symbol"], decision="SKIP", reason="no direction")

    producer.model.decide.side_effect = abstain
    assert not await producer.cycle("all-skip")
    assert producer.model.decide.await_count == 2
    assert not any(p["symbol"] == "HEDGEUSDT" for p in publications(producer))
    assert producer.store.rows(
        "SELECT * FROM incidents WHERE scope='PRIMARY_DIRECTION' AND status='OPEN'"
    )


async def test_empty_normal_selection_is_failure_even_if_extra_hedge_analyzed(producer):
    producer.screening.select.return_value = []
    assert not await producer.cycle("empty")
    assert producer.model.decide.await_count == 1
    assert publications(producer)[0]["normal_candidate"] is False


async def test_results_are_published_individually_but_one_cycle_notification(producer):
    assert await producer.cycle("merged")
    assert len(publications(producer)) == 2
    assert not producer.store.rows("SELECT * FROM outbox WHERE event_key LIKE 'analysis:%'")
    rows = producer.store.rows("SELECT * FROM outbox WHERE event_key='cycle:merged'")
    assert len(rows) == 1 and "北京时间" in rows[0]["text"]
    assert "HEDGEUSDT" in rows[0]["text"] and "OTHERUSDT" in rows[0]["text"]
    await producer.cycle("merged")
    assert len(producer.store.rows("SELECT * FROM outbox WHERE event_key='cycle:merged'")) == 1


async def test_automatic_long_cycle_does_not_immediately_run_again(producer, monkeypatch):
    clock = [1200.0]
    monkeypatch.setattr('time.time', lambda: clock[0])
    original = producer.model.decide.side_effect

    async def slow(sid, context):
        result = await original(sid, context)
        clock[0] += 1300
        return result

    producer.model.decide.side_effect = slow
    # Market timestamps remain fresh relative to the fake clock.
    async def evidence(symbol, candidate):
        return dict(symbol=symbol, observed_at=datetime.fromtimestamp(clock[0]+1300, UTC).isoformat())

    producer.markets.evidence.side_effect = evidence
    producer.store.set('next_analysis_at', 1200)
    assert await producer.cycle()
    calls = producer.model.decide.await_count
    assert producer.store.state('next_analysis_at') == 4800
    await producer.cycle()
    assert producer.model.decide.await_count == calls
