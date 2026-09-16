import json
from decimal import Decimal as D

import pytest

from longtime.execution import Executor
from longtime.monitor import Monitor


async def test_open_uses_real_fill_reduce_only_and_static_protection(setup):
    e, s, x, m, d = setup
    assert await e.enter("123", "sig", d) == "OPEN"
    t = s.rows("SELECT * FROM trades")[0]
    assert t["entry_price"] == "100"
    assert t["leverage"] == "3"
    assert D(t["margin"]) == 10
    assert x.account_calls == 2
    assert len(x.submissions) == 3
    entry, sl, tp = x.submissions
    assert entry["orderType"] == "Market" and not entry["reduceOnly"]
    assert sl["orderType"] == "Market" and sl["reduceOnly"] and sl["closeOnTrigger"]
    assert sl["triggerDirection"] == 2
    assert tp["orderType"] == "Limit" and tp["timeInForce"] == "GTC" and tp["reduceOnly"]
    await Monitor(e).tick()
    assert len(x.submissions) == 3


async def test_existing_position_skips_all_economics(setup):
    e, s, x, m, d = setup
    x.position_rows = [{"symbol": "TESTUSDT", "size": "1"}]
    assert await e.enter("123", "sig", d) == "SKIP_EXISTING_POSITION"
    assert not x.submissions and x.account_calls == 0


async def test_ambiguous_entry_reconciles_without_duplicate(setup):
    e, s, x, m, d = setup
    x.ambiguous_entry = True
    assert await e.enter("123", "sig", d) == "OPEN"
    assert len([o for o in x.submissions if not o["reduceOnly"]]) == 1


async def test_protection_fails_twice_then_manual_once_and_restart_does_not_reset(setup):
    e, s, x, m, d = setup
    x.fail_kind = "SL"
    assert await e.enter("123", "sig", d) == "OPEN"
    t = s.rows("SELECT * FROM trades")[0]
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 2
    assert t["tp_order_id"] and t["sl_order_id"] is None
    recreated = Executor(e.settings, s, x, m)
    await Monitor(recreated).tick()
    await Monitor(recreated).tick()
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 2
    alerts = s.rows("SELECT * FROM incidents WHERE scope LIKE 'SL:%'")
    assert len(alerts) == 1
    assert json.loads(
        s.rows("SELECT markup FROM outbox WHERE event_key LIKE 'alert:%'")[0]["markup"]
    )["inline_keyboard"]
    assert not await recreated.protect(t["trade_id"], manual_kind="SL")
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 3
    x.fail_kind = None
    assert await recreated.protect(t["trade_id"], manual_kind="SL")
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 4
    assert await recreated.protect(t["trade_id"], manual_kind="SL")
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 4


async def test_closed_position_retry_never_opens_reverse(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.position_rows = []
    before = len(x.submissions)
    assert await e.protective_order(t["trade_id"], "SL", manual=True)
    assert len(x.submissions) == before


async def test_margin_reserve_blocks_before_any_order(setup):
    e, s, x, m, d = setup
    x.balance = D("30")
    assert await e.enter("123", "sig", d) == "STOP_INSUFFICIENT_MARGIN"
    assert not x.submissions


async def test_fee_and_slippage_reserve_and_each_entry_refresh(setup):
    e, s, x, m, d = setup
    x.balance = D("40.01")
    assert await e.enter("123", "sig", d) == "OPEN"
    s.signal("sig2", "123", "SECONDUSDT", {})
    d = d.model_copy(update={"symbol": "SECONDUSDT"})
    assert await e.enter("123", "sig2", d) == "STOP_INSUFFICIENT_MARGIN"
    assert x.account_calls >= 3


async def test_dry_run_has_no_mutations(setup):
    e, s, x, m, d = setup
    e.settings.trading_enabled = False
    assert await e.enter("123", "sig", d) == "DRY_RUN_ELIGIBLE"
    assert not x.submissions


async def test_settlement_uses_exchange_net_once_and_cancels_sibling(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t, pnl="0.477")
    monitor = Monitor(e)
    await monitor.tick()
    closed = s.trade(t["trade_id"])
    assert closed["status"] == "CLOSED"
    assert D(closed["net_pnl"]) == D(".477")  # must not subtract fees from exchange closedPnl again
    assert D(closed["fees"]) > 0
    assert closed["close_reason"] == "TP"
    assert closed["holding_duration"] == pytest.approx(60, abs=0.01)
    assert x.cancels == [t["sl_order_id"]]
    await monitor.tick()
    assert len(s.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")) == 1


async def test_partial_settlement_never_fabricates_full_close(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t, partial=True)
    await Monitor(e).tick()
    assert s.trade(t["trade_id"])["status"] == "SETTLING"
    assert not s.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")


async def test_changed_position_identity_refuses_manual_protection(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.position_rows[0]["avgPrice"] = "95"
    with pytest.raises(ValueError, match="身份"):
        await e.protective_order(t["trade_id"], "SL", manual=True)
    assert len(x.submissions) == 3


async def test_lower_supported_leverage_preserves_ten_margin(setup):
    e, s, x, m, d = setup
    x.leverage = D(2)
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    assert D(t["leverage"]) == 2 and D(t["margin"]) == 10 and D(t["qty"]) == D(".2")


async def test_rejected_protection_gets_only_one_automatic_replacement(setup):
    e, s, x, m, d = setup
    original = x.submit

    async def reject(payload):
        oid = await original(payload)
        if payload.get("closeOnTrigger"):
            x.orders[payload["orderLinkId"]]["orderStatus"] = "Rejected"
        return oid

    x.submit = reject
    await e.enter("123", "sig", d)
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 2
    assert s.rows("SELECT SUM(attempts) AS n FROM orders WHERE kind='SL'")[0]["n"] == 2
    await Monitor(e).tick()
    assert len([o for o in x.submissions if o.get("closeOnTrigger")]) == 2


async def test_partial_open_gets_stop_without_topup(setup):
    e, s, x, m, d = setup
    original = x.submit

    async def partial(payload):
        if not payload["reduceOnly"]:
            payload = {**payload, "qty": "0.01"}
        return await original(payload)

    x.submit = partial
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    assert t["status"] == "OPEN" and D(t["qty"]) == D(".01")
    assert D(t["sl_price"]) > 0
    assert len([o for o in x.submissions if not o["reduceOnly"]]) == 1


async def test_under_sized_protection_is_not_reported_as_protected(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    order = next(o for o in x.orders.values() if o["orderId"] == t["tp_order_id"])
    order["leavesQty"] = "0.001"
    with pytest.raises(ValueError, match="quantity"):
        e.verify_protection(t, "TP", order)


async def test_existing_external_missing_protection_retry_is_readonly(setup):
    e, s, x, m, d = setup
    x.position_rows = [
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
    monitor = Monitor(e)
    await monitor.tick()
    assert not await monitor.check_external("TESTUSDT")
    assert not x.submissions
    x.position_rows[0].update(takeProfit="101", stopLoss="90")
    assert await monitor.check_external("TESTUSDT")
    assert not s.rows("SELECT * FROM incidents WHERE status='OPEN'")


async def test_partial_exit_evidence_survives_exchange_history_expiry(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t, partial=True)
    original_row = dict(x.pnl_rows[0])
    original_order = next(
        dict(o) for o in x.orders.values() if o["orderId"] == original_row["orderId"]
    )
    monitor = Monitor(e)
    await monitor.capture_settlement(t)
    # The earlier partial exit is no longer returned by the exchange.
    x.pnl_rows = [{**original_row, "orderId": "manual-final", "closedPnl": "0.2"}]
    x.orders = {
        "manual-final": {
            **original_order,
            "orderId": "manual-final",
            "orderLinkId": "external-final",
        }
    }
    x.exit_execs = {
        "manual-final": [
            {
                "orderId": "manual-final",
                "execType": "Trade",
                "execQty": original_row["closedSize"],
                "execFee": "0.01",
                "execTime": original_row["updatedTime"],
            }
        ]
    }
    s.update_trade(t["trade_id"], status="SETTLING")
    assert await monitor.settle(t["trade_id"])
    final = s.trade(t["trade_id"])
    assert final["status"] == "CLOSED"
    assert D(final["net_pnl"]) == D("0.697")
    assert final["close_reason"] == "MIXED"


async def test_lost_ack_recovered_does_not_emit_unnecessary_alert(setup):
    e, s, x, m, d = setup
    x.ambiguous_entry = True
    assert await e.enter("123", "sig", d) == "OPEN"
    assert not s.rows("SELECT * FROM outbox WHERE event_key LIKE 'alert:%'")
    assert s.rows("SELECT * FROM events WHERE kind='ENTRY_ACK_UNCERTAIN'")


async def test_exit_between_position_and_order_read_does_not_false_alarm(setup):
    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    trade = s.rows("SELECT * FROM trades")[0]
    original = x.order

    async def order(symbol, link):
        x.position_rows = []
        result = await original(symbol, link)
        if result:
            result["orderStatus"] = "Cancelled"
        return result

    x.order = order
    await Monitor(e).tick()
    assert not s.rows("SELECT * FROM incidents WHERE scope LIKE 'SL:%' OR scope LIKE 'TP:%'")
    assert s.trade(trade["trade_id"])["status"] == "OPEN"


async def test_delayed_history_creation_filter_recovers_old_protection(setup):
    import time

    e, s, x, m, d = setup
    await e.enter("123", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t, pnl="0.5249215")
    details = json.loads(t["details"])
    details.update(flat_detected_at=time.time() - 200, settlement_cursor=time.time() + 1000)
    s.update_trade(t["trade_id"], status="SETTLING", details=json.dumps(details))
    created = int(t["opened_at"] * 1000)

    async def history(path, symbol, start, end):
        if start > created:
            return []
        return x.pnl_rows if path.endswith("closed-pnl") else list(x.orders.values())

    x.history_window = history
    assert await Monitor(e).settle(t["trade_id"])
    assert s.trade(t["trade_id"])["net_pnl"] == "0.5249215"
    await Monitor(e).tick()
    assert len(s.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")) == 1


async def test_repeated_same_incident_does_not_flood_error_events(setup):
    e, s, x, m, d = setup
    for _ in range(5):
        e.alert("SETTLEMENT:test", "monitor", {}, RuntimeError("same error"))
    assert len(s.rows("SELECT * FROM events WHERE kind='ERROR'")) == 1
    assert len(s.rows("SELECT * FROM incidents")) == 1


@pytest.mark.parametrize("budget", [None, "1", "2", "4", "2.7"])
async def test_recovered_intent_uses_persisted_stop_budget(setup, monkeypatch, budget):
    e, s, x, m, d = setup
    original = e.reconcile_entry

    async def delayed(_):
        return False

    monkeypatch.setattr(e, "reconcile_entry", delayed)
    assert await e.enter("123", "sig", d) == "INTENT"
    t = s.rows("SELECT * FROM trades")[0]
    details = json.loads(t["details"])
    assert details["sl_loss"] == "2.7" and t["tp_target_net_pnl"] == "0.5"
    if budget is None:
        details.pop("sl_loss")  # Pre-change intent retains its original 8U budget.
    else:
        details["sl_loss"] = budget
    s.update_trade(t["trade_id"], details=json.dumps(details))
    assert await original(t["trade_id"])
    t = s.trade(t["trade_id"])
    details = json.loads(t["details"])
    entry, qty, stop = D(t["entry_price"]), D(t["qty"]), D(t["sl_price"])
    net = qty * (stop - entry) - D(details["entry_fee"])
    net -= qty * stop * D(details["taker"]) + entry * qty * D(".0005")
    loss = D(budget or "8")
    assert -loss <= net <= -loss + D(".01")
    prices = (t["tp_price"], t["sl_price"])
    count = len(x.submissions)
    await Monitor(Executor(e.settings, s, x, m)).startup()
    after = s.trade(t["trade_id"])
    assert (after["tp_price"], after["sl_price"]) == prices
    assert len(x.submissions) == count


async def test_confirmed_order_fee_protects_before_execution_history_arrives(setup):
    e, s, x, m, decision = setup
    submit = x.submit
    executions = x.executions

    async def aggregate(payload):
        oid = await submit(payload)
        if not payload["reduceOnly"]:
            row = x.orders[payload["orderLinkId"]]
            value = D(payload["qty"]) * 100
            row.update(
                cumExecValue=str(value),
                cumFeeDetail={"USDT": str(value * D(".00055"))},
                updatedTime=row["createdTime"],
            )
        return oid

    async def unavailable(*args, **kwargs):
        raise AssertionError("Protection must not wait for execution history")

    x.submit = aggregate
    x.executions = unavailable
    assert await e.enter("cycle", "sig", decision) == "OPEN"
    t = s.rows("SELECT * FROM trades")[0]
    d = json.loads(t["details"])
    assert d["entry_evidence_pending"] and d["entry_fee_source"] == "order_cumFeeDetail"
    assert t["sl_order_id"] and t["tp_order_id"]
    prices = t["sl_price"], t["tp_price"]
    x.executions = executions
    await Monitor(e).tick()
    after = s.trade(t["trade_id"])
    assert not json.loads(after["details"])["entry_evidence_pending"]
    assert (after["sl_price"], after["tp_price"]) == prices
    assert len(x.submissions) == 3


async def test_crossed_stop_rejection_is_persisted_and_reason_survives_monitor(setup):
    from longtime.transport import BybitAPIError

    e, s, x, m, decision = setup
    submit = x.submit
    attempts = []

    async def reject(payload):
        if payload.get("closeOnTrigger"):
            attempts.append(payload)
            raise BybitAPIError(110093, "expect Falling", method="POST", path="/v5/order/create")
        return await submit(payload)

    x.submit = reject
    assert await e.enter("cycle", "sig", decision) == "OPEN"
    t = s.rows("SELECT * FROM trades")[0]
    row = s.rows("SELECT * FROM orders WHERE kind='SL'")[0]
    assert row["status"] == "Rejected" and row["attempts"] == 2
    assert json.loads(row["receipt"])["retCode"] == 110093
    await Monitor(e).tick()
    assert len(attempts) == 2
    incident = s.rows("SELECT * FROM incidents WHERE scope LIKE 'SL:%'")[0]
    assert "原触发价已被行情越过" in incident["error"]
    x.submit = submit
    assert await e.protect(t["trade_id"], manual_kind="SL")
    after = s.trade(t["trade_id"])
    assert after["sl_price"] == t["sl_price"] and after["sl_order_id"]


@pytest.mark.parametrize(
    "side,kind,last",
    [
        ("LONG", "SL", "50"),
        ("SHORT", "SL", "150"),
        ("LONG", "TP", "110"),
        ("SHORT", "TP", "90"),
    ],
)
async def test_crossed_original_target_uses_reduce_only_market(setup, side, kind, last):
    e, s, x, m, d = setup
    d = d.model_copy(update={"decision": side})
    await e.enter("cycle", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    old = next(o for o in x.orders.values() if o["orderId"] == t[kind.lower() + "_order_id"])
    old["orderStatus"] = "Cancelled"

    async def price(symbol):
        return D(last)

    x.last_price = price
    # Partial remaining position: market exit must never use original full qty.
    x.position_rows[0]["size"] = str(D(t["qty"]) / 2)
    assert await e.protect(t["trade_id"], manual_kind=kind)
    payload = x.submissions[-1]
    assert payload["orderType"] == "Market" and payload["reduceOnly"] and payload["closeOnTrigger"]
    assert payload["qty"] == x.position_rows[0]["size"]
    assert "triggerPrice" not in payload and "price" not in payload
    assert payload["side"] == ("Sell" if side == "LONG" else "Buy")
    before = len(x.submissions)
    await e.protect(t["trade_id"], manual_kind=kind)
    assert len(x.submissions) == before


async def test_uncertain_crossed_market_retry_keeps_identical_payload(setup):
    e, s, x, m, d = setup
    await e.enter("cycle", "sig", d)
    t = s.rows("SELECT * FROM trades")[0]
    old = next(o for o in x.orders.values() if o["orderId"] == t["sl_order_id"])
    old["orderStatus"] = "Cancelled"

    async def price(symbol):
        return D(50)

    x.last_price = price
    payloads = []

    async def uncertain(payload):
        payloads.append(payload)
        raise RuntimeError("unknown response")

    x.submit = uncertain
    assert not await e.protect(t["trade_id"], manual_kind="SL")
    assert not await e.protect(t["trade_id"], manual_kind="SL")
    assert payloads[0] == payloads[1] and "triggerPrice" not in payloads[0]


@pytest.mark.parametrize("missing", ["pnl", "executions"])
async def test_settlement_grace_survives_restart_and_alerts_once(setup, monkeypatch, missing):
    import time

    e, s, x, _, decision = setup
    await e.enter("123", "sig", decision)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t)
    pnl, executions = x.pnl_rows, x.exit_execs
    if missing == "pnl":
        x.pnl_rows = []
    else:
        x.exit_execs = {}
    now = time.time()
    monkeypatch.setattr("longtime.monitor.time.time", lambda: now)
    await Monitor(e).tick()
    now += 210
    await Monitor(e).tick()
    assert s.trade(t["trade_id"])["status"] == "SETTLING"
    assert not s.rows("SELECT * FROM incidents WHERE scope LIKE 'SETTLEMENT:%'")
    now += 90
    await Monitor(e).tick()
    await Monitor(e).tick()
    assert len(s.rows("SELECT * FROM incidents WHERE scope LIKE 'SETTLEMENT:%'")) == 1
    assert len(s.rows("SELECT * FROM outbox WHERE event_key LIKE 'alert:%'")) == 1
    x.pnl_rows, x.exit_execs = pnl, executions
    await Monitor(e).tick()
    assert s.trade(t["trade_id"])["status"] == "CLOSED"
    assert not s.rows("SELECT * FROM incidents WHERE status='OPEN'")
    assert len(s.rows("SELECT * FROM outbox WHERE event_key LIKE 'closed:%'")) == 1


async def test_settlement_sync_within_grace_only_sends_close(setup, monkeypatch):
    import time

    e, s, x, _, decision = setup
    await e.enter("123", "sig", decision)
    t = s.rows("SELECT * FROM trades")[0]
    x.close_at_tp(t)
    pnl = x.pnl_rows
    x.pnl_rows = []
    now = time.time()
    monkeypatch.setattr("longtime.monitor.time.time", lambda: now)
    await Monitor(e).tick()
    now += 210
    x.pnl_rows = pnl
    await Monitor(e).tick()
    assert s.trade(t["trade_id"])["status"] == "CLOSED"
    assert not s.rows("SELECT * FROM incidents")


async def test_entry_scientific_decimal_is_persisted_before_submission(setup, monkeypatch):
    from dataclasses import replace

    from longtime.transport import BybitAPIError

    e, s, x, m, d = setup
    original = x.instrument

    async def instrument(symbol):
        return replace(await original(symbol), step=D("1E+1"), min_qty=D("1E+1"))

    async def quote(symbol):
        return D("0.25"), D("0.25")

    async def submit(payload):
        assert payload["qty"] == "120"
        stored = s.rows("SELECT payload FROM orders WHERE link_id=?", (payload["orderLinkId"],))
        assert json.loads(stored[0]["payload"]) == payload
        raise BybitAPIError(10001, "test rejection", method="POST", path="/v5/order/create")

    x.instrument, x.quote, x.submit = instrument, quote, submit
    monkeypatch.setattr("longtime.execution.reachable_tp", lambda *args, **kwargs: {"target": "0.5"})
    await e.enter("fixed", "fixed-sig", d)
    assert json.loads(s.rows("SELECT payload FROM orders")[0]["payload"])["qty"] == "120"


async def test_new_protection_intent_formats_small_frozen_price(setup):
    e, s, x, m, d = setup
    await e.enter("fixed", "fixed-sig", d)
    trade = s.rows("SELECT * FROM trades")[0]
    s.execute("UPDATE trades SET tp_price='1E-7' WHERE trade_id=?", (trade["trade_id"],))
    old = s.rows("SELECT * FROM orders WHERE kind='TP'")[0]
    x.orders[old["link_id"]]["orderStatus"] = "Cancelled"
    s.execute("UPDATE orders SET status='Cancelled' WHERE link_id=?", (old["link_id"],))

    async def below_target(symbol):
        return D("1E-8")

    x.last_price = below_target
    await e.protective_order(trade["trade_id"], "TP", manual=True)
    latest = s.rows("SELECT payload FROM orders WHERE kind='TP' ORDER BY created_at DESC")[0]
    assert json.loads(latest["payload"])["price"] == "0.0000001"
