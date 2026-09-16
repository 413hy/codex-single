import copy
import json
import time
from decimal import Decimal as D

import pytest
from conftest import FakeExchange, FakeMarkets

from longtime.config import Settings
from longtime.hedge import HedgeExecutor, distance_price
from longtime.model import Decision
from longtime.store import Store


class HedgeExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.idx = 1
        self.fill_times = {}
        self.lost_hedge_ack = False
        self.fill_on_cancel = False

    async def submit(self, payload):
        if payload["orderType"] == "Limit" and not payload["reduceOnly"]:
            self.submissions.append(copy.deepcopy(payload))
            oid = "oid-" + payload["orderLinkId"]
            self.orders[payload["orderLinkId"]] = dict(
                payload,
                orderId=oid,
                orderStatus="Untriggered",
                cumExecQty="0",
                avgPrice="0",
                createdTime=str(int(time.time() * 1000)),
                updatedTime=str(int(time.time() * 1000)),
            )
            if self.lost_hedge_ack:
                raise RuntimeError("ack lost")
            return oid
        if payload["reduceOnly"] and payload["orderType"] == "Market":
            self.submissions.append(copy.deepcopy(payload))
            oid = "oid-" + payload["orderLinkId"]
            self.orders[payload["orderLinkId"]] = dict(
                payload,
                orderId=oid,
                orderStatus="Filled",
                cumExecQty=payload["qty"],
                avgPrice="100",
            )
            self.position_rows = [
                p for p in self.position_rows if p["positionIdx"] != payload["positionIdx"]
            ]
            return oid
        return await super().submit(payload)

    def fill(self, link, quantity, *, at=None):
        o = self.orders[link]
        o.update(
            cumExecQty=str(quantity),
            avgPrice=o["price"],
            orderStatus="Filled" if D(quantity) == D(o["qty"]) else "PartiallyFilled",
        )
        idx = o["positionIdx"]
        self.position_rows = [p for p in self.position_rows if p["positionIdx"] != idx]
        self.position_rows.append(
            dict(
                symbol=o["symbol"],
                side=o["side"],
                positionIdx=idx,
                size=str(quantity),
                avgPrice=o["price"],
                leverage="3",
                takeProfit="0",
                stopLoss="0",
            )
        )
        self.fill_times.setdefault(o["orderId"], at or time.time())

    async def executions(self, symbol, oid, at=None):
        if oid in self.fill_times:
            o = next(o for o in self.orders.values() if o["orderId"] == oid)
            return [
                dict(
                    orderId=oid,
                    execType="Trade",
                    execQty=o["cumExecQty"],
                    execFee="0.001",
                    execTime=str(int(self.fill_times[oid] * 1000)),
                )
            ]
        return await super().executions(symbol, oid, at)

    async def cancel(self, symbol, oid):
        if self.fill_on_cancel:
            o = next(o for o in self.orders.values() if o["orderId"] == oid)
            if not o["reduceOnly"]:
                self.fill(o["orderLinkId"], o["qty"])
                return
        await super().cancel(symbol, oid)


@pytest.fixture
async def hedge(tmp_path):
    store = Store(tmp_path / "test.db")
    ex = HedgeExchange()
    executor = HedgeExecutor(
        Settings(_env_file=None, trading_enabled=True), store, ex, FakeMarkets()
    )
    store.signal("s", "c", "TESTUSDT", {})
    await executor.enter("c", "s", Decision(symbol="TESTUSDT", decision="LONG", reason="test"))
    g = executor.hedge.groups()[0]
    child = store.trade(g["child"])
    return executor, store, ex, g, json.loads(child["details"])["entry_link"]


def notices(store):
    return [
        r["event_key"]
        for r in store.rows("SELECT event_key FROM outbox WHERE event_key LIKE 'hedge:%'")
    ]


def test_distances():
    assert distance_price("LONG", D(100), D(".3"), D("1.7"), D(".01"), favorable=False) == D(
        "94.34"
    )
    assert distance_price("SHORT", D(100), D(".3"), D("1.7"), D(".01"), favorable=False) == D(
        "105.66"
    )
    assert distance_price("LONG", D(100), D(".3"), D(".5"), D(".01"), favorable=True) == D("101.67")


async def test_order_geometry_and_partial_keeps_tp(hedge):
    e, store, ex, g, link = hedge
    o = ex.orders[link]
    assert o["reduceOnly"] is False and o["positionIdx"] == 2
    assert o["orderType"] == "Limit" and o["price"] == o["triggerPrice"]
    assert o["qty"] == store.trade(g["active"])["qty"]
    assert not store.rows("SELECT * FROM orders WHERE kind='SL'")
    ex.fill(link, ".1")
    await e.hedge.tick()
    assert e.hedge.get(g["group_id"])["phase"] == "SINGLE"
    assert not ex.cancels and not notices(store)


async def test_fill_within_30_seconds_only_full(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ".1")
    await e.hedge.tick()
    ex.fill(link, ex.orders[link]["qty"])
    await e.hedge.tick()
    assert e.hedge.get(g["group_id"])["phase"] == "LOCKED"
    assert len(notices(store)) == 1 and notices(store)[0].endswith(":full")
    assert ex.cancels
    before = len(ex.submissions)
    await e.hedge.tick()
    assert len(ex.submissions) == before and len(notices(store)) == 1


async def test_slow_partial_notice_and_restart_dedup(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ".1", at=time.time() - 31)
    await e.hedge.tick()
    assert len(notices(store)) == 1 and notices(store)[0].endswith(":partial")
    restarted = HedgeExecutor(e.settings, store, ex, FakeMarkets())
    await restarted.hedge.tick()
    assert len(notices(store)) == 1
    ex.fill(link, ex.orders[link]["qty"])
    await restarted.hedge.tick()
    assert len(notices(store)) == 2


@pytest.mark.parametrize("fill_on_cancel", [False, True])
async def test_tp_first_cancels_then_closes_actual_reverse_quantity(hedge, fill_on_cancel):
    e, store, ex, g, link = hedge
    ex.fill(link, ".1")
    ex.position_rows = [p for p in ex.position_rows if p["positionIdx"] == 2]
    ex.fill_on_cancel = fill_on_cancel
    await e.hedge.tick()
    assert e.hedge.get(g["group_id"])["phase"] == "DONE"
    closes = [o for o in ex.submissions if o["reduceOnly"] and o["orderType"] == "Market"]
    assert len(closes) == 1
    assert D(closes[0]["qty"]) == (D(ex.orders[link]["qty"]) if fill_on_cancel else D(".1"))
    assert not ex.position_rows
    await e.hedge.tick()
    assert len([o for o in ex.submissions if o["orderType"] == "Market" and o["reduceOnly"]]) == 1


async def test_skip_direction_leaves_locked_pair(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    await e.hedge.tick()
    before = len(ex.submissions)
    result = await e.hedge.decide(
        g["group_id"], "review", Decision(symbol="TESTUSDT", decision="SKIP", reason="无方向")
    )
    assert result == "SKIP_MODEL" and len(ex.submissions) == before


async def test_unreachable_direction_does_not_close_reverse(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    await e.hedge.tick()

    async def no_candles(symbol):
        return []

    e.markets.reachability = no_candles
    before = len(ex.submissions)
    result = await e.hedge.decide(
        g["group_id"], "review", Decision(symbol="TESTUSDT", decision="LONG", reason="test")
    )
    assert result == "SKIP_TP_UNREACHABLE"
    assert len(ex.submissions) == before


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_direction_closes_loser_rebases_and_arms_next_hedge(hedge, side):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    await e.hedge.tick()
    before = len(ex.submissions)
    decision = Decision(symbol="TESTUSDT", decision=side, reason="test")
    result = await e.hedge.decide(g["group_id"], "review", decision)
    assert result == "HEDGE_DIRECTION_APPLIED"
    new = e.hedge.get(g["group_id"])
    assert new["phase"] == "SINGLE" and new["generation"] == 1
    active = store.trade(new["active"])
    assert active["side"] == side
    submitted = ex.submissions[before:]
    assert submitted[0]["reduceOnly"] and submitted[0]["orderType"] == "Market"
    assert len(submitted) == 3
    assert submitted[1]["reduceOnly"] is True and submitted[1]["orderType"] == "Limit"
    assert submitted[2]["reduceOnly"] is False and submitted[2]["orderType"] == "Limit"
    reference = D(new["reference"])
    qty = D(active["qty"])
    pnl_distance = (D(active["tp_price"]) - reference) * qty * (1 if side == "LONG" else -1)
    assert D(".5") <= pnl_distance <= D(".5") + D(".01") * qty
    assert store.trade(new["loser"])["status"] == "SETTLING"
    await e.hedge.tick()
    assert len(ex.submissions) == before + 3


async def test_ambiguous_hedge_submit_recovers_without_duplicate(tmp_path):
    store = Store(tmp_path / "test.db")
    ex = HedgeExchange()
    ex.lost_hedge_ack = True
    e = HedgeExecutor(Settings(_env_file=None, trading_enabled=True), store, ex, FakeMarkets())
    store.signal("s", "c", "TESTUSDT", {})
    await e.enter("c", "s", Decision(symbol="TESTUSDT", decision="LONG", reason="test"))
    assert e.hedge.groups()[0]["phase"] == "ARMING"
    restarted = HedgeExecutor(e.settings, store, ex, FakeMarkets())
    await restarted.hedge.tick()
    assert restarted.hedge.groups()[0]["phase"] == "SINGLE"
    hedges = [o for o in ex.submissions if not o["reduceOnly"] and o["orderType"] == "Limit"]
    assert len(hedges) == 1


async def test_full_hedge_cancels_tp_even_when_execution_history_is_unavailable(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])

    async def unavailable(*args, **kwargs):
        raise RuntimeError("execution history temporarily unavailable")

    ex.executions = unavailable
    await e.hedge.tick()
    tp = next(o for o in ex.orders.values() if o["reduceOnly"])
    assert tp["orderStatus"] == "Cancelled"
    assert e.hedge.get(g["group_id"])["phase"] == "LOCKING"


async def test_recovered_slow_fill_emits_partial_then_full(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    base = time.time() - 60
    oid = ex.orders[link]["orderId"]
    original = ex.executions

    async def fills(symbol, order_id, at=None):
        if order_id != oid:
            return await original(symbol, order_id, at)
        return [
            dict(
                orderId=oid,
                execType="Trade",
                execQty=qty,
                execFee=".001",
                execTime=str(int(ts * 1000)),
            )
            for qty, ts in [(".1", base), (str(D(ex.orders[link]["qty"]) - D(".1")), base + 45)]
        ]

    ex.executions = fills
    await e.hedge.tick()
    keys = notices(store)
    assert len(keys) == 2 and any(k.endswith(":partial") for k in keys)


async def test_pending_fill_notice_keeps_original_generation_after_redecision(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    # An order aggregate allows trading to progress while the history endpoint is unavailable.
    child = store.trade(g["child"])
    o = ex.orders[link]
    o.update(cumFeeDetail={"USDT": "0.001"}, cumExecValue=str(D(o["price"]) * D(o["qty"])))
    history = ex.executions

    async def unavailable(*args, **kwargs):
        raise RuntimeError("history down")

    ex.executions = unavailable
    await e.hedge.tick()
    assert e.hedge.get(g["group_id"])["phase"] == "LOCKED"
    await e.hedge.decide(
        g["group_id"], "review", Decision(symbol="TESTUSDT", decision="LONG", reason="test")
    )
    assert e.hedge.get(g["group_id"])["generation"] == 1
    ex.executions = history
    await e.hedge.tick()
    assert f"hedge:{g['group_id']}:0:full" in notices(store)
    assert f"hedge:{g['group_id']}:1:full" not in notices(store)
    assert store.trade(child["trade_id"])["status"] == "SETTLING"


async def test_generation_is_rechecked_inside_execution_lock(hedge):
    e, store, ex, g, link = hedge
    ex.fill(link, ex.orders[link]["qty"])
    await e.hedge.tick()
    before = len(ex.submissions)
    result = await e.hedge.decide(
        g["group_id"],
        "old",
        Decision(symbol="TESTUSDT", decision="LONG", reason="test"),
        expected_generation=-1,
    )
    assert result == "SKIP_HEDGE_GENERATION" and len(ex.submissions) == before


async def test_tp_fills_before_hedge_submit_never_opens_orphan(tmp_path):
    store = Store(tmp_path / 'test.db')
    ex = HedgeExchange()
    submit = ex.submit

    async def immediate_tp(payload):
        oid = await submit(payload)
        if payload['reduceOnly'] and payload['orderType'] == 'Limit':
            ex.orders[payload['orderLinkId']].update(orderStatus='Filled', cumExecQty=payload['qty'])
            ex.position_rows = []
        return oid

    ex.submit = immediate_tp
    e = HedgeExecutor(Settings(_env_file=None, trading_enabled=True), store, ex, FakeMarkets())
    store.signal('s', 'c', 'TESTUSDT', {})
    await e.enter('c', 's', Decision(symbol='TESTUSDT', decision='LONG', reason='test'))
    g = e.hedge.groups()[0]
    assert g['phase'] == 'DONE'
    assert store.trade(g['child'])['status'] == 'UNFILLED'
    assert not [p for p in ex.submissions if not p['reduceOnly'] and p['orderType'] == 'Limit']
    await e.hedge.tick()
    assert not ex.position_rows


async def test_tp_unknown_ack_does_not_submit_hedge_and_restart_cleans_unsent_child(tmp_path):
    store = Store(tmp_path / 'test.db')
    ex = HedgeExchange()
    submit = ex.submit

    async def lost_tp_ack(payload):
        oid = await submit(payload)
        if payload['reduceOnly'] and payload['orderType'] == 'Limit':
            raise RuntimeError('TP accepted but response lost')
        return oid

    ex.submit = lost_tp_ack
    e = HedgeExecutor(Settings(_env_file=None, trading_enabled=True), store, ex, FakeMarkets())
    store.signal('s', 'c', 'TESTUSDT', {})
    await e.enter('c', 's', Decision(symbol='TESTUSDT', decision='LONG', reason='test'))
    assert e.hedge.groups()[0]['phase'] == 'ARMING'
    assert not [p for p in ex.submissions if not p['reduceOnly'] and p['orderType'] == 'Limit']
    for o in ex.orders.values():
        if o['reduceOnly']:
            o.update(orderStatus='Filled', cumExecQty=o['qty'])
    ex.position_rows = []
    restarted = HedgeExecutor(e.settings, store, ex, FakeMarkets())
    await restarted.hedge.tick()
    await restarted.hedge.tick()
    g = restarted.hedge.groups()[0]
    assert g['phase'] == 'DONE' and store.trade(g['child'])['status'] == 'UNFILLED'
    assert len(ex.submissions) == 2
