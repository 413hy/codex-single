"""Failure-boundary regressions from the 2026-09-11 system audit."""

import asyncio

import pytest

from longtime.config import Settings
from longtime.monitor import Monitor
from longtime.store import Store
from longtime.telegram import Telegram


async def test_monitor_late_failure_does_not_resolve_and_recreate_incident(setup):
    e, store, exchange, _, _ = setup
    monitor = Monitor(e)
    calls = 0

    async def positions():
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise RuntimeError("final position snapshot failed")
        return []

    exchange.active_positions = positions
    store.set("monitor_heartbeat", 123)
    for _ in range(3):
        with pytest.raises(RuntimeError) as failure:
            await monitor.tick()
        e.alert("MONITOR", "monitor", {}, failure.value)
    assert len(store.rows("SELECT * FROM incidents WHERE scope='MONITOR'")) == 1
    assert store.state("monitor_heartbeat") == 123


async def test_delivery_interruption_consumes_durable_budget(tmp_path):
    store = Store(tmp_path / "db")
    settings = Settings(_env_file=None, runtime_dir=tmp_path)
    store.queue("message", "test")
    calls = 0

    async def interrupted(*args):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    for _ in range(2):
        bot = Telegram(settings, Store(store.path))
        bot.call = interrupted
        with pytest.raises(asyncio.CancelledError):
            await bot.deliver()
        await bot.close()
    bot = Telegram(settings, Store(store.path))
    bot.call = interrupted
    try:
        await bot.deliver()
        row = store.rows("SELECT status,attempts FROM outbox WHERE event_key='message'")[0]
        assert row == {"status": "FAILED", "attempts": 2}
        assert calls == 2
    finally:
        await bot.close()




async def test_external_position_protection_is_rechecked_without_writes(setup):
    e, store, exchange, _, _ = setup
    exchange.position_rows = [
        {
            "symbol": "EXTERNALUSDT",
            "positionIdx": 0,
            "side": "Buy",
            "size": "1",
            "avgPrice": "100",
            "takeProfit": "110",
            "stopLoss": "90",
        }
    ]
    monitor = Monitor(e)
    await monitor.tick()
    assert not store.rows("SELECT * FROM incidents WHERE scope='EXTERNAL:EXTERNALUSDT:0'")
    exchange.position_rows[0]["stopLoss"] = "0"
    await monitor.tick()
    await monitor.tick()
    assert (
        len(
            store.rows(
                "SELECT * FROM incidents WHERE scope='EXTERNAL:EXTERNALUSDT:0' AND status='OPEN'"
            )
        )
        == 1
    )
    assert not exchange.submissions and not exchange.cancels


async def test_external_check_does_not_classify_owned_slot_as_external(setup):
    e, store, exchange, _, decision = setup
    await e.enter("test", "sig", decision)
    exchange.orders = {}
    exchange.position_rows.append(
        {
            "symbol": "TESTUSDT",
            "positionIdx": 2,
            "side": "Sell",
            "size": "1",
            "avgPrice": "100",
            "takeProfit": "90",
            "stopLoss": "110",
        }
    )
    assert await Monitor(e).check_external("TESTUSDT")
    assert not store.rows("SELECT * FROM incidents WHERE scope LIKE 'EXTERNAL:%'")


async def test_concurrent_delivery_sends_one_event_once(tmp_path):
    store = Store(tmp_path / "db")
    store.queue("event", "test")
    bot = Telegram(Settings(_env_file=None, runtime_dir=tmp_path), store)
    sent = []

    async def send(*args):
        sent.append(args)
        await asyncio.sleep(0)
        return {"message_id": 1}

    bot.call = send
    try:
        await asyncio.gather(bot.deliver(), bot.deliver())
        assert len(sent) == 1
        assert store.rows("SELECT status,attempts FROM outbox") == [
            {"status": "SENT", "attempts": 1}
        ]
    finally:
        await bot.close()








async def test_recovered_intent_missing_fill_evidence_alerts_once_without_resubmit(setup):
    from longtime.store import Store

    e, store, exchange, _, decision = setup
    exchange.fail_kind = "ENTRY"
    await e.enter("test", "sig", decision)
    trade = store.rows("SELECT * FROM trades")[0]
    # Simulate the crash window before enter() reaches its final alert.
    store.execute("DELETE FROM incidents WHERE scope=?", ("ENTRY:" + trade["trade_id"],))
    store.execute("DELETE FROM outbox")
    # Exchange did fill, but both fee sources are unavailable after restart.
    import json

    exchange.fail_kind = None
    await exchange.submit(
        json.loads(store.rows("SELECT payload FROM orders WHERE kind='ENTRY'")[0]["payload"])
    )

    async def missing_executions(*args, **kwargs):
        return []

    exchange.executions = missing_executions
    submissions = len(exchange.submissions)
    e.store = Store(store.path)
    monitor = Monitor(e)
    for _ in range(3):
        await monitor.tick()
    assert len(exchange.submissions) == submissions
    assert (
        len(
            store.rows(
                "SELECT * FROM incidents WHERE scope=? AND status='OPEN'",
                ("ENTRY:" + trade["trade_id"],),
            )
        )
        == 1
    )
    assert store.trade(trade["trade_id"])["status"] == "INTENT"


