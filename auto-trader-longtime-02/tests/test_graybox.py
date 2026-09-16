"""Real executor, HMAC transport, SQLite, monitor and Telegram with wire-level fault injection.

Only the remote HTTP server is replaced. No real trading credentials or orders.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal as D

import httpx
import pytest
from conftest import FakeExchange, FakeMarkets

from longtime.config import Settings
from longtime.exchange import Exchange
from longtime.execution import Executor
from longtime.model import Decision
from longtime.monitor import Monitor
from longtime.store import Store
from longtime.telegram import Telegram


class DemoWire:
    def __init__(self):
        self.remote = FakeExchange()
        self.calls = []
        self.margin_mode = "REGULAR_MARGIN"
        self.fail_sl = False
        self.lose_entry_response = False
        self.lost = False
        self.bad_json = False

    async def __call__(self, req):
        path = req.url.path
        params = dict(req.url.params) if req.method == "GET" else json.loads(req.content)
        self.calls.append((req.method, path, params))
        assert req.url.host == "api-demo.bybit.com"
        if path.startswith(("/v5/account/", "/v5/position/", "/v5/order/", "/v5/execution/")):
            data = req.url.query.decode() if req.method == "GET" else req.content.decode()
            signed = req.headers["X-BAPI-TIMESTAMP"] + "gray-key" + "10000" + data
            assert (
                req.headers["X-BAPI-SIGN"]
                == hmac.new(b"gray-secret", signed.encode(), hashlib.sha256).hexdigest()
            )
        if self.bad_json:
            return httpx.Response(502, text="<html>bad gateway</html>")
        result = {}
        if path == "/v5/market/time":
            result = {"timeNano": str(time.time_ns())}
        elif path == "/v5/account/info":
            result = {"marginMode": self.margin_mode}
        elif path == "/v5/account/wallet-balance":
            result = {
                "list": [
                    {
                        "totalAvailableBalance": str(self.remote.balance),
                        "totalWalletBalance": "100",
                        "accountIMRate": "0.1",
                        "accountMMRate": "0.02",
                    }
                ]
            }
        elif path == "/v5/position/list":
            rows = await self.remote.positions(params.get("symbol"))
            if params.get("symbol") and not rows:
                rows = [
                    {
                        "symbol": params["symbol"],
                        "positionIdx": idx,
                        "size": "0",
                        "leverage": str(self.remote.leverage),
                    } for idx in ((1, 2) if self.remote.idx else (0,))
                ]
            result = {"list": rows}
        elif path == "/v5/position/set-leverage":
            self.remote.leverage = D(params["buyLeverage"])
        elif path == "/v5/market/instruments-info":
            result = {
                "list": [
                    {
                        "symbol": params["symbol"],
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "settleCoin": "USDT",
                        "quoteCoin": "USDT",
                        "priceFilter": {"tickSize": ".01"},
                        "lotSizeFilter": {
                            "qtyStep": ".001",
                            "minOrderQty": ".001",
                            "minNotionalValue": "5",
                            "maxMktOrderQty": "10000",
                            "maxOrderQty": "10000",
                        },
                        "leverageFilter": {"maxLeverage": "5", "leverageStep": ".01"},
                    }
                ]
            }
        elif path == "/v5/market/tickers":
            result = {
                "list": [
                    {
                        "symbol": params["symbol"],
                        "bid1Price": "99.99",
                        "ask1Price": "100",
                        "lastPrice": "100",
                    }
                ]
            }
        elif path == "/v5/order/create":
            if self.fail_sl and params.get("closeOnTrigger"):
                return httpx.Response(200, json={"retCode": 10001, "retMsg": "injected rejection"})
            oid = await self.remote.submit(params)
            if self.lose_entry_response and not params["reduceOnly"] and not self.lost:
                self.lost = True
                raise httpx.ReadTimeout("injected lost acknowledgement", request=req)
            result = {"orderId": oid}
        elif path in ("/v5/order/realtime", "/v5/order/history"):
            if params.get("orderLinkId"):
                o = await self.remote.order(params.get("symbol"), params["orderLinkId"])
                rows = [o] if o else []
            elif params.get("openOnly") == "0":
                rows = await self.remote.open_orders(params.get("symbol"))
            else:
                rows = list(self.remote.orders.values())
            result = {"list": rows}
        elif path == "/v5/execution/list":
            result = {"list": await self.remote.executions(params.get("symbol"), params["orderId"])}
        elif path == "/v5/position/closed-pnl":
            result = {"list": self.remote.pnl_rows}
        elif path == "/v5/order/cancel":
            await self.remote.cancel(params["symbol"], params["orderId"])
        else:
            pytest.fail("Unexpected API call: " + path)
        return httpx.Response(
            200,
            json={"retCode": 0, "retMsg": "OK", "result": result, "time": int(time.time() * 1000)},
        )


@pytest.fixture
async def gray(tmp_path, monkeypatch):
    import asyncio

    original = asyncio.sleep

    async def fast(_):
        await original(0)

    monkeypatch.setattr(asyncio, "sleep", fast)
    cfg = Settings(
        _env_file=None,
        trading_enabled=True,
        bybit_api_key="gray-key",
        bybit_api_secret="gray-secret",
        runtime_dir=tmp_path,
        telegram_token="gray-token",
        telegram_chat_id=10,
        telegram_user_id=20,
    )
    wire = DemoWire()
    client = httpx.AsyncClient(
        base_url="https://api-demo.bybit.com", transport=httpx.MockTransport(wire)
    )
    exchange = Exchange(cfg, client)
    store = Store(tmp_path / "gray.db")
    engine = Executor(cfg, store, exchange, FakeMarkets())
    store.signal("gray-signal", "gray-cycle", "TESTUSDT", {})
    decision = Decision(symbol="TESTUSDT", decision="LONG", reason="灰盒固定模型输入")
    yield engine, store, wire, decision
    await exchange.close()


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_wire_lifecycle_restart_and_outbox(gray, side):
    engine, store, wire, decision = gray
    decision = decision.model_copy(update={"decision": side})
    wire.lose_entry_response = True
    await engine.exchange.sync_clock()
    assert await engine.enter("gray-cycle", "gray-signal", decision) == "OPEN"
    trade = store.rows("SELECT * FROM trades")[0]
    assert trade["tp_order_id"] and trade["sl_order_id"]
    assert (
        len(
            [
                p
                for method, path, p in wire.calls
                if path == "/v5/order/create" and not p["reduceOnly"]
            ]
        )
        == 1
    )
    # Reopen the SQLite connection via a fresh Store/Executor, preserving remote state.
    restored = Executor(engine.settings, Store(store.path), engine.exchange, engine.markets)
    await Monitor(restored).startup()
    assert len(wire.remote.submissions) == 3
    wire.remote.close_at_tp(trade, pnl="0.4321")
    await Monitor(restored).tick()
    assert store.trade(trade["trade_id"])["net_pnl"] == "0.4321"
    assert wire.remote.cancels == [trade["sl_order_id"]]
    messages = []

    async def telegram_wire(req):
        assert req.url.host == "api.telegram.org"
        body = json.loads(req.content)
        messages.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(messages)}})

    bot = Telegram(
        engine.settings, store, httpx.AsyncClient(transport=httpx.MockTransport(telegram_wire))
    )
    await bot.deliver()
    await bot.deliver()
    assert len(messages) == 2  # One confirmed open and one settled close, even after restart.
    direction = "做多" if side == "LONG" else "做空"
    assert messages[0]["text"].startswith("✅ 开仓 · TESTUSDT · " + direction)
    assert "止盈止损已挂好" in messages[0]["text"]
    assert messages[1]["text"].startswith("💰 止盈 · TESTUSDT")
    assert "净收益 +0.4321U" in messages[1]["text"]
    await bot.close()


async def test_wire_protection_rejection_button_retry(gray):
    engine, store, wire, decision = gray
    wire.fail_sl = True
    await engine.enter("gray-cycle", "gray-signal", decision)
    trade = store.rows("SELECT * FROM trades")[0]
    assert (
        len(
            [
                p
                for _, path, p in wire.calls
                if path == "/v5/order/create" and p.get("closeOnTrigger")
            ]
        )
        == 2
    )
    restored = Executor(engine.settings, Store(store.path), engine.exchange, engine.markets)
    await Monitor(restored).startup()
    assert (
        len(
            [
                p
                for _, path, p in wire.calls
                if path == "/v5/order/create" and p.get("closeOnTrigger")
            ]
        )
        == 2
    )
    wire.fail_sl = False
    incident = store.rows("SELECT * FROM incidents WHERE scope LIKE 'SL:%'")[0]
    sent = []

    async def telegram_wire(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    bot = Telegram(
        engine.settings, store, httpx.AsyncClient(transport=httpx.MockTransport(telegram_wire))
    )

    async def retry(row, callback_id):
        payload = json.loads(row["payload"])
        return await restored.protect(payload["trade_id"], manual_kind=payload["kind"])

    update = {
        "callback_query": {
            "id": "gray-callback",
            "from": {"id": 20},
            "message": {"chat": {"id": 10}},
            "data": "retry:" + incident["incident_id"],
        }
    }
    await bot.handle(update, retry)
    await __import__("asyncio").gather(*bot.tasks)
    await bot.handle(update, retry)
    assert store.trade(trade["trade_id"])["sl_order_id"]
    assert (
        len(
            [
                p
                for _, path, p in wire.calls
                if path == "/v5/order/create" and p.get("closeOnTrigger")
            ]
        )
        == 3
    )
    await bot.close()


@pytest.mark.parametrize("failure", ["json", "margin"])
async def test_wire_account_fault_never_creates_order(gray, failure):
    engine, store, wire, decision = gray
    if failure == "json":
        wire.bad_json = True
    else:
        wire.margin_mode = "ISOLATED_MARGIN"
    with pytest.raises((ValueError, RuntimeError)):
        await engine.enter("gray-cycle", "gray-signal", decision)
    assert not any(path == "/v5/order/create" for _, path, _ in wire.calls)


async def test_pause_while_account_request_is_in_flight(setup):
    engine, store, remote, market, decision = setup
    original = remote.account

    async def account():
        result = await original()
        if remote.account_calls == 2:
            store.set("entries_paused", True)
        return result

    remote.account = account
    assert await engine.enter("123", "sig", decision) == "SKIP_PAUSED"
    assert remote.submissions == []




async def test_gray_settlement_database_failure_rolls_back_notification(gray):
    engine, store, wire, decision = gray
    await engine.enter("gray-cycle", "gray-signal", decision)
    trade = store.rows("SELECT * FROM trades")[0]
    wire.remote.close_at_tp(trade)
    store.execute(
        "CREATE TRIGGER fail_closed_outbox BEFORE INSERT ON outbox WHEN NEW.event_key LIKE 'closed:%' BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END"
    )
    await Monitor(engine).tick()
    assert store.trade(trade["trade_id"])["status"] == "SETTLING"
    assert not store.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")
    store.execute("DROP TRIGGER fail_closed_outbox")
    await Monitor(engine).tick()
    assert store.trade(trade["trade_id"])["status"] == "CLOSED"
    assert len(store.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")) == 1




@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
async def test_actual_hedge_executor_signed_wire_full_lock_and_redecision(gray, side):
    from test_hedge_strategy import HedgeExchange

    from longtime.hedge import HedgeExecutor

    base, store, wire, decision = gray
    wire.remote = HedgeExchange()
    engine = HedgeExecutor(base.settings, store, base.exchange, base.markets)
    decision = decision.model_copy(update={'decision': side})
    assert await engine.enter('gray-cycle', 'gray-signal', decision) == 'OPEN'
    group = engine.hedge.groups()[0]
    child = store.trade(group['child'])
    link = json.loads(child['details'])['entry_link']
    wire.remote.fill(link, wire.remote.orders[link]['qty'])
    await engine.hedge.tick()
    group = engine.hedge.get(group['group_id'])
    assert group['phase'] == 'LOCKED'
    restarted = HedgeExecutor(base.settings, Store(store.path), base.exchange, base.markets)
    before = len(wire.remote.submissions)
    await restarted.hedge.tick()
    assert len(wire.remote.submissions) == before
    reverse = 'SHORT' if side == 'LONG' else 'LONG'
    outcome = await restarted.hedge.decide(
        group['group_id'], 'wire-review', decision.model_copy(update={'decision':reverse}),
        expected_generation=group['generation'],
    )
    assert outcome == 'HEDGE_DIRECTION_APPLIED'
    current = restarted.hedge.get(group['group_id'])
    assert current['generation'] == 1 and current['phase'] == 'SINGLE'
    assert store.trade(current['active'])['side'] == reverse
    assert not store.rows("SELECT * FROM orders WHERE kind='SL'")
    requests = [p for _, path, p in wire.calls if path == '/v5/order/create']
    assert all(p['orderType']=='Limit' for p in requests if not p['reduceOnly'] and 'triggerPrice' in p)
    assert any(p['reduceOnly'] and p['orderType']=='Market' for p in requests)
