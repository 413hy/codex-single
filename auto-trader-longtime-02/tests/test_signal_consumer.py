import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from longtime.config import Settings
from longtime.service import App
from longtime.store import Store


def publication(path, *, normal=True, hedge=None, decision="LONG", age=0, sid="one"):
    now = time.time() - age
    payload = dict(
        version=1,
        signal_id=sid,
        symbol="TESTUSDT",
        decision=decision,
        reason="validated market structure",
        normal_candidate=normal,
        hedge=hedge,
        observed_at="2026-09-15T10:00:00+00:00",
        published_at=now,
        expires_at=now + 60,
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS publications(id INTEGER PRIMARY KEY,signal_id TEXT,symbol TEXT,published_at REAL,expires_at REAL,payload TEXT)"
        )
        db.execute(
            "INSERT INTO publications(signal_id,symbol,published_at,expires_at,payload) VALUES (?,?,?,?,?)",
            (sid, "TESTUSDT", now, now + 60, json.dumps(payload)),
        )


@pytest.fixture
def consumer(tmp_path):
    settings = Settings(
        _env_file=None, runtime_dir=tmp_path / "trader", signal_db=tmp_path / "signals.db"
    )
    app = App(settings, exchange=AsyncMock(), markets=AsyncMock())
    app.executor.enter = AsyncMock(return_value="OPEN")
    return app


async def test_two_traders_process_same_signal_independently_once(consumer):
    app = consumer
    publication(app.settings.signal_db)
    second = App(
        app.settings,
        store=Store(app.settings.runtime_dir / "second.db"),
        exchange=AsyncMock(),
        markets=AsyncMock(),
    )
    second.executor.enter = AsyncMock(return_value="OPEN")
    await app.consumer.tick()
    await second.consumer.tick()
    await app.consumer.tick()
    await second.consumer.tick()
    assert app.executor.enter.await_count == second.executor.enter.await_count == 1
    restarted = App(app.settings, exchange=AsyncMock(), markets=AsyncMock())
    restarted.executor.enter = AsyncMock()
    await restarted.consumer.tick()
    restarted.executor.enter.assert_not_awaited()


@pytest.mark.parametrize(
    "age,decision,normal,paused",
    [
        (61, "LONG", True, False),
        (0, "SKIP", True, False),
        (0, "LONG", False, False),
        (0, "LONG", True, True),
    ],
)
async def test_expired_skip_extra_and_paused_never_enter(consumer, age, decision, normal, paused):
    publication(consumer.settings.signal_db, age=age, decision=decision, normal=normal)
    consumer.store.set("entries_paused", paused)
    await consumer.consumer.tick()
    consumer.executor.enter.assert_not_awaited()


async def test_failed_submission_signal_is_not_replayed(consumer):
    publication(consumer.settings.signal_db)
    consumer.executor.enter.side_effect = RuntimeError("uncertain submission")
    await consumer.consumer.tick()
    await consumer.consumer.tick()
    assert consumer.executor.enter.await_count == 1
    assert consumer.store.rows("SELECT status FROM signals")[0]["status"] == "ERROR"


async def test_readonly_missing_feed_does_not_create_database(consumer):
    with pytest.raises(sqlite3.OperationalError):
        await consumer.consumer.tick()
    assert not consumer.settings.signal_db.exists()


async def test_deadline_passed_to_executor(consumer):
    publication(consumer.settings.signal_db)
    await consumer.consumer.tick()
    deadline = consumer.executor.enter.call_args.kwargs["deadline"]
    assert 59 < deadline - time.time() <= 60


async def test_no_analysis_methods_or_frequency_controls(consumer):
    from longtime import settings_controls

    assert not hasattr(consumer, "model") and not hasattr(consumer, "screening")
    assert "cycle_minutes" not in settings_controls.LABELS
    text, markup = settings_controls.panel(
        __import__("longtime.trading_settings", fromlist=["EntryDefaults"]).EntryDefaults()
    )
    assert "settings:edit:cycle_minutes" not in json.dumps(markup)
    with pytest.raises(ValueError):
        settings_controls.candidate_for(None, "cycle_minutes", 20)


async def test_hedge_generation_matching_and_pause(consumer):
    if not hasattr(consumer.executor, "hedge"):
        return
    from unittest.mock import Mock

    engine = consumer.executor.hedge
    engine.get = Mock(
        return_value=dict(symbol="TESTUSDT", phase="LOCKED", generation=2, group_id="group")
    )
    engine.decide = AsyncMock(return_value="HEDGE_DIRECTION_APPLIED")
    publication(
        consumer.settings.signal_db, normal=False, hedge=dict(group_id="group", generation=2)
    )
    consumer.store.set("entries_paused", True)
    await consumer.consumer.tick()
    engine.decide.assert_awaited_once()
    consumer.executor.enter.assert_not_awaited()
    publication(
        consumer.settings.signal_db,
        normal=False,
        hedge=dict(group_id="group", generation=1),
        sid="stale-group",
    )
    await consumer.consumer.tick()
    assert engine.decide.await_count == 1


async def test_subscription_drives_real_hedge_executor_through_redecision(tmp_path):
    from conftest import FakeMarkets
    from test_hedge_strategy import HedgeExchange

    settings = Settings(
        _env_file=None,
        runtime_dir=tmp_path / "trader",
        signal_db=tmp_path / "feed.db",
        trading_enabled=True,
    )
    ex = HedgeExchange()
    app = App(settings, exchange=ex, markets=FakeMarkets())
    publication(settings.signal_db)
    await app.consumer.tick()
    g = app.executor.hedge.groups()[0]
    assert g["phase"] == "SINGLE"
    child = app.store.trade(g["child"])
    link = json.loads(child["details"])["entry_link"]
    ex.fill(link, ex.orders[link]["qty"])
    await app.monitor.tick()
    g = app.executor.hedge.get(g["group_id"])
    assert g["phase"] == "LOCKED"
    app.store.set("entries_paused", True)
    reachability = app.markets.reachability
    app.markets.reachability = AsyncMock(return_value=[])
    publication(
        settings.signal_db,
        normal=False,
        hedge={"group_id": g["group_id"], "generation": g["generation"]},
        sid="unreachable-review",
    )
    count = len(ex.submissions)
    await app.consumer.tick()
    rejected = app.store.rows("SELECT * FROM signals WHERE signal_id='feed:unreachable-review'")[0]
    assert rejected["status"] == "SKIP_TP_UNREACHABLE" and rejected["side"] == "SKIP"
    assert len(ex.submissions) == count
    app.markets.reachability = reachability
    publication(
        settings.signal_db,
        normal=False,
        hedge={"group_id": g["group_id"], "generation": g["generation"]},
        sid="review",
    )
    await app.consumer.tick()
    g = app.executor.hedge.get(g["group_id"])
    assert g["phase"] == "SINGLE" and g["generation"] == 1
    count = len(ex.submissions)
    await app.consumer.tick()
    assert len(ex.submissions) == count
    assert not app.store.rows("SELECT * FROM orders WHERE kind='SL'")


async def test_new_round_same_symbol_is_not_deduplicated(consumer):
    publication(consumer.settings.signal_db, sid="round1")
    await consumer.consumer.tick()
    publication(consumer.settings.signal_db, sid="round2")
    await consumer.consumer.tick()
    assert consumer.executor.enter.await_count == 2
    assert len(consumer.store.rows("SELECT * FROM signals")) == 2


async def test_shutdown_records_interruption_without_replaying_claim(consumer):
    import asyncio

    consumer.executor.enter.side_effect = asyncio.CancelledError()
    publication(consumer.settings.signal_db, sid='shutdown')
    with pytest.raises(asyncio.CancelledError):
        await consumer.consumer.tick()
    row = consumer.store.rows("SELECT status FROM signals WHERE signal_id='feed:shutdown'")[0]
    assert row['status'] == 'INTERRUPTED'
    consumer.executor.enter.side_effect = None
    await consumer.consumer.tick()
    assert consumer.executor.enter.await_count == 1
    assert consumer.store.rows("SELECT * FROM events WHERE kind='SIGNAL_INTERRUPTED'")
