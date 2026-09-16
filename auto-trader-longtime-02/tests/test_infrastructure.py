import hashlib
import hmac
from decimal import Decimal as D

import httpx
import pytest

from longtime.config import Settings
from longtime.exchange import Exchange
from longtime.service import ProcessLock, cycle_id
from longtime.store import Store
from longtime.telegram import Telegram
from longtime.transport import DemoTransport


def test_demo_transport_rejects_mainnet_and_testnet():
    for url in (
        "https://api.bybit.com",
        "https://api-testnet.bybit.com",
        "https://api-demo.bybit.com.evil.invalid",
    ):
        with pytest.raises(ValueError, match="Demo"):
            DemoTransport(Settings(_env_file=None), httpx.AsyncClient(base_url=url))


async def test_signing_reuses_exact_bytes_and_private_host():
    seen = []

    async def handler(request):
        seen.append(request)
        assert request.url.host == "api-demo.bybit.com"
        data = request.url.query.decode() if request.method == "GET" else request.content.decode()
        signed = request.headers["X-BAPI-TIMESTAMP"] + "test-key" + "10000" + data
        assert (
            request.headers["X-BAPI-SIGN"]
            == hmac.new(b"test-secret", signed.encode(), hashlib.sha256).hexdigest()
        )
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(handler)
    )
    ex = Exchange(
        Settings(
            _env_file=None,
            bybit_api_key="test-key",
            bybit_api_secret="test-secret",
            trading_enabled=True,
        ),
        client,
    )
    await ex._private("GET", "/v5/position/list", {"symbol": "TESTUSDT", "category": "linear"})
    await ex._private("POST", "/v5/order/create", {"qty": "0.5", "reduceOnly": True})
    assert seen[0].url.query == b"category=linear&symbol=TESTUSDT"
    with pytest.raises(ValueError):
        await ex._private("GET", "https://api.bybit.com/v5/position/list")
    await ex.close()


async def test_private_pagination_and_repeat_cursor_rejected():
    ex = Exchange(Settings(_env_file=None))
    calls = []

    async def private(method, path, params):
        calls.append(params.copy())
        return {
            "result": {
                "list": [{"symbol": "A" if len(calls) == 1 else "B"}],
                "nextPageCursor": "next" if len(calls) == 1 else "",
            }
        }

    ex._private = private
    assert len(await ex.rows("/v5/position/list", {})) == 2
    assert calls[1]["cursor"] == "next"

    async def repeat(*args):
        return {"result": {"list": [], "nextPageCursor": "same"}}

    ex._private = repeat
    with pytest.raises(ValueError, match="Repeated"):
        await ex.rows("/v5/position/list", {})
    await ex.close()


def test_cycle_claim_and_process_lock_survive_multiple_instances(tmp_path):
    store = Store(tmp_path / "test.db")
    assert store.claim_cycle("10")
    assert not Store(store.path).claim_cycle("10")
    lock = ProcessLock(tmp_path / "lock")
    with pytest.raises(RuntimeError, match="Another"):
        ProcessLock(tmp_path / "lock")
    lock.close()
    assert cycle_id(1200) == cycle_id(2399) == "1"
    assert cycle_id(2400) == "2"


def test_incident_dedup_and_callback_claim_are_durable(tmp_path):
    store = Store(tmp_path / "test.db")
    first = store.incident("SL:X", "protection", {}, "fail")
    assert store.incident("SL:X", "protection", {}, "fail again") == first
    assert len(store.rows("SELECT * FROM outbox")) == 1
    assert store.claim_callback("callback1", first)
    assert not store.claim_callback("callback1", first)
    assert not store.claim_callback("callback2", first)
    store.resolve("SL:X")
    second = store.incident("SL:X", "protection", {}, "new occurrence")
    assert second != first


async def test_telegram_denies_unauthorized_and_repeated_callbacks(tmp_path):
    store = Store(tmp_path / "test.db")
    settings = Settings(
        _env_file=None, telegram_token="TEST-NOT-REAL", telegram_chat_id=1, telegram_user_id=2
    )
    bot = Telegram(settings, store)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    bot.call = call
    iid = store.incident("SL:X", "protection", {"trade_id": "x"}, "fail")
    retries = []

    async def retry(row, callback_id):
        retries.append(callback_id)
        return True

    update = {
        "callback_query": {
            "id": "a",
            "from": {"id": 99},
            "message": {"chat": {"id": 1}},
            "data": "retry:" + iid,
        }
    }
    await bot.handle(update, retry)
    assert not retries and calls[-1][1]["text"] == "无权限"
    update["callback_query"]["from"]["id"] = 2
    await bot.handle(update, retry)
    await __import__("asyncio").gather(*bot.tasks)
    await bot.handle(update, retry)
    assert retries == ["a"]
    await bot.close()






async def test_uncertain_entry_keeps_symbol_reserved_and_button_never_resends(setup):
    e, s, x, m, d = setup
    x.fail_kind = "ENTRY"
    assert await e.enter("123", "sig", d) == "INTENT"
    assert "TESTUSDT" in s.reserved()
    t = s.rows("SELECT * FROM trades")[0]
    assert not await e.reconcile_entry(t["trade_id"])
    assert len(x.submissions) == 1


async def test_short_hedge_mode_uses_correct_slot_and_trigger(setup):
    e, s, x, m, d = setup
    x.idx = 2
    d = d.model_copy(update={"decision": "SHORT"})
    assert await e.enter("123", "sig", d) == "OPEN"
    entry, sl, tp = x.submissions
    assert entry["positionIdx"] == 2 and entry["side"] == "Sell"
    assert sl["triggerDirection"] == 1 and sl["side"] == "Buy" and sl["positionIdx"] == 2
    assert D(sl["triggerPrice"]) > D(100) > D(tp["price"])


async def test_readonly_transport_rejects_every_private_mutation():
    ex = Exchange(Settings(_env_file=None))
    with pytest.raises(ValueError, match="TRADING_ENABLED"):
        await ex._private("POST", "/v5/order/create", {})
    with pytest.raises(ValueError, match="TRADING_ENABLED"):
        await ex._private("POST", "/v5/order/cancel", {})
    await ex.close()


async def test_database_failure_can_alert_without_database(setup):
    import sqlite3

    from longtime import emergency

    e, s, x, m, d = setup

    def broken(*args):
        raise sqlite3.OperationalError("disk failure")

    s.event = broken
    e.alert("CYCLE", "cycle", {}, RuntimeError("failure"))
    assert emergency.load(e.settings.runtime_dir)["sent"] is False
    bot = Telegram(e.settings, s)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {"message_id": 1}

    bot.call = call
    await bot.deliver_emergency()
    await bot.deliver_emergency()
    assert len(calls) == 1
    assert calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "retry:database"
    await bot.close()


async def test_callback_ack_failure_does_not_strand_retry(tmp_path):
    import asyncio

    store = Store(tmp_path / "db")
    settings = Settings(_env_file=None, telegram_chat_id=1, telegram_user_id=2)
    bot = Telegram(settings, store)

    async def failed_ack(*args):
        raise RuntimeError("expired callback")

    bot.call = failed_ack
    iid = store.incident("X", "monitor", {}, "failure")
    seen = []

    async def retry(*args):
        seen.append(1)
        return True

    await bot.handle(
        {
            "callback_query": {
                "id": "q",
                "from": {"id": 2},
                "message": {"chat": {"id": 1}},
                "data": "retry:" + iid,
            }
        },
        retry,
    )
    await asyncio.gather(*bot.tasks)
    assert seen == [1]
    assert store.rows("SELECT status FROM incidents")[0]["status"] == "RESOLVED"
    await bot.close()
