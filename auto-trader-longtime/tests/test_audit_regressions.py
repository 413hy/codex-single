"""Regression evidence for the file-by-file audit, without exchange mutations."""


import httpx
import pytest

from longtime.config import Settings
from longtime.monitor import Monitor
from longtime.transport import DemoTransport


async def test_entry_deadline_checked_after_slow_preparation(setup):
    e, store, exchange, _, decision = setup
    assert await e.enter("cycle", "sig", decision, deadline=1) == "SKIP_CYCLE_ENDED"
    assert not exchange.submissions
    assert not store.rows("SELECT * FROM trades")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "get"])
async def test_readonly_blocks_all_non_get_methods(method):
    ex = DemoTransport(Settings(_env_file=None))
    try:
        with pytest.raises(ValueError, match="TRADING_ENABLED"):
            await ex._private(method, "/v5/order/create", {})
    finally:
        await ex.close()


@pytest.mark.parametrize(
    "body", [[], {"retCode": 0}, {"retCode": 500}, {"retMsg": "upstream error"}]
)
async def test_http_error_is_not_definitive_order_rejection(body):
    from longtime.transport import BybitAPIError

    client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(lambda req: httpx.Response(503, json=body)),
    )
    ex = DemoTransport(Settings(_env_file=None, trading_enabled=True), client)
    try:
        with pytest.raises(BybitAPIError) as error:
            await ex._private("POST", "/v5/order/create", {})
        assert not error.value.definitive_rejection
    finally:
        await ex.close()


async def test_accepted_entry_with_http_error_stays_single_order(setup):
    from longtime.transport import _validate_document

    e, store, exchange, _, decision = setup
    original = exchange.submit

    async def submit(payload):
        result = await original(payload)
        if not payload["reduceOnly"]:
            _validate_document({}, 503, method="POST", path="/v5/order/create")
        return result

    exchange.submit = submit
    assert await e.enter("cycle", "sig", decision) == "OPEN"
    assert len([o for o in exchange.submissions if not o["reduceOnly"]]) == 1


async def test_closed_during_monitor_not_recorded_as_external(setup):
    e, store, exchange, _, decision = setup
    await e.enter("cycle", "sig", decision)
    t = store.rows("SELECT * FROM trades")[0]
    original = exchange.positions
    fired = False

    async def positions(symbol=None):
        nonlocal fired
        if symbol and not fired:
            fired = True
            exchange.close_at_tp(t)
        return await original(symbol)

    exchange.positions = positions
    await Monitor(e).tick()
    assert store.trade(t["trade_id"])["status"] == "CLOSED"
    assert not store.rows("SELECT * FROM events WHERE kind='EXTERNAL_POSITION_OBSERVED'")
    assert store.state("exchange_positions") == []


async def test_delivery_success_does_not_hide_other_failed_messages(tmp_path):
    from longtime.store import Store
    from longtime.telegram import Telegram

    store = Store(tmp_path / "db")
    store.queue("failed", "old")
    store.execute("UPDATE outbox SET status='FAILED',attempts=2 WHERE event_key='failed'")
    store.incident("TELEGRAM_DELIVERY", "delivery", {}, "delivery failed")
    store.queue("new", "new")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
    )
    bot = Telegram(Settings(_env_file=None), store, client)
    try:
        await bot.deliver()
        assert store.rows("SELECT status FROM incidents")[0]["status"] == "OPEN"
        store.execute("UPDATE outbox SET status='PENDING',attempts=0 WHERE event_key='failed'")
        await bot.deliver()
        assert store.rows("SELECT status FROM incidents")[0]["status"] == "RESOLVED"
    finally:
        await bot.close()


def test_changed_incident_error_is_retained_and_deduplicated(setup):
    e, store, _, _, _ = setup
    for error in ("first", "changed", "changed"):
        e.alert("CANDIDATE:TESTUSDT", "candidate", {"symbol": "TESTUSDT"}, RuntimeError(error))
    assert store.rows("SELECT error FROM incidents") == [{"error": "changed"}]
    assert len(store.rows("SELECT * FROM events WHERE kind='ERROR'")) == 2
    assert len(store.rows("SELECT * FROM outbox")) == 1


async def test_database_callback_replay_does_not_clear_new_emergency(tmp_path):
    from longtime import emergency
    from longtime.store import Store
    from longtime.telegram import Telegram

    store = Store(tmp_path / "db")
    settings = Settings(
        _env_file=None, runtime_dir=tmp_path, telegram_user_id=1, telegram_chat_id=2
    )
    bot = Telegram(settings, store)

    async def call(*args):
        return {}

    bot.call = call
    update = {
        "callback_query": {
            "id": "db-callback",
            "from": {"id": 1},
            "message": {"chat": {"id": 2}},
            "data": "retry:database",
        }
    }
    try:
        emergency.queue(tmp_path, "old", "error")
        await bot.handle(update, None)
        assert emergency.load(tmp_path) is None
        emergency.queue(tmp_path, "new", "error")
        await bot.handle(update, None)
        assert emergency.load(tmp_path)["scope"] == "new"
    finally:
        await bot.close()


async def test_partial_tp_with_real_remaining_position_is_observed_without_topup(setup):
    e, store, exchange, _, decision = setup
    await e.enter("cycle", "sig", decision)
    t = store.rows("SELECT * FROM trades")[0]
    position = dict(exchange.position_rows[0])
    exchange.close_at_tp(t, partial=True)
    from decimal import Decimal

    position["size"] = str(Decimal(t["qty"]) / 2)
    exchange.position_rows = [position]
    tp = next(o for o in exchange.orders.values() if o["orderId"] == t["tp_order_id"])
    tp["orderStatus"] = "PartiallyFilled"
    tp["leavesQty"] = position["size"]
    await Monitor(e).tick()
    assert store.trade(t["trade_id"])["status"] == "OPEN"
    assert len(exchange.submissions) == 3
    assert not store.rows("SELECT * FROM incidents WHERE scope LIKE 'TP:%'")
    tp["orderStatus"] = "Filled"
    await Monitor(e).tick()
    assert store.rows("SELECT * FROM incidents WHERE scope LIKE 'TP:%'")
    assert len(exchange.submissions) == 3




@pytest.mark.parametrize("terminal", ["Cancelled", "Rejected"])
async def test_manual_protection_late_rejection_submits_only_once(setup, terminal):
    e, store, exchange, _, decision = setup
    exchange.fail_kind = "SL"
    await e.enter("cycle", "sig", decision)
    t = store.rows("SELECT * FROM trades")[0]
    before = len(exchange.submissions)
    exchange.fail_kind = None
    original_submit = exchange.submit

    async def submit(payload):
        oid = await original_submit(payload)
        exchange.orders[payload["orderLinkId"]]["orderStatus"] = terminal
        return oid

    exchange.submit = submit
    assert not await e.protect(t["trade_id"], manual_kind="SL")
    assert len(exchange.submissions) == before + 1


async def test_external_native_stop_can_lock_in_profit_without_adoption(setup):
    e, store, exchange, _, _ = setup
    exchange.position_rows = [
        {
            "symbol": "TESTUSDT",
            "positionIdx": 0,
            "side": "Buy",
            "size": "1",
            "avgPrice": "100",
            "markPrice": "120",
            "takeProfit": "130",
            "stopLoss": "110",
        }
    ]
    assert await Monitor(e).check_external("TESTUSDT")
    assert not exchange.submissions and not store.rows("SELECT * FROM trades")


async def test_external_uncovered_or_wrong_trigger_order_is_not_confirmed(setup):
    e, store, exchange, _, _ = setup
    exchange.position_rows = [
        {
            "symbol": "TESTUSDT",
            "positionIdx": 0,
            "side": "Buy",
            "size": "1",
            "avgPrice": "100",
            "takeProfit": "0",
            "stopLoss": "0",
        }
    ]
    exchange.orders = {
        "tp": {
            "symbol": "TESTUSDT",
            "positionIdx": 0,
            "side": "Sell",
            "qty": "0.1",
            "orderStatus": "New",
            "reduceOnly": True,
            "orderType": "Limit",
            "price": "110",
        },
        "sl": {
            "symbol": "TESTUSDT",
            "positionIdx": 0,
            "side": "Sell",
            "qty": "1",
            "orderStatus": "Untriggered",
            "reduceOnly": True,
            "orderType": "Market",
            "triggerPrice": "110",
            "triggerDirection": 1,
            "closeOnTrigger": True,
        },
    }
    assert not await Monitor(e).check_external("TESTUSDT")
    assert not exchange.submissions




