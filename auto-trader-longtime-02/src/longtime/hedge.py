"""Durable two-leg hedge lifecycle. All mutations run under Executor.lock."""

from __future__ import annotations

import json
import time
from decimal import Decimal as D

from longtime.exchange import LIVE, TERMINAL
from longtime.execution import Executor, matches_position
from longtime.risk import ceil, floor, number, reachable_tp
from longtime.store import encode, identity
from longtime.transport import BybitAPIError


def distance_price(side, reference, qty, amount, tick, *, favorable):
    sign = D(1 if side == "LONG" else -1)
    raw = reference + sign * amount / qty * (1 if favorable else -1)
    # TP rounds outwards; hedge rounds towards the reference (at most one tick).
    price = ceil(raw, tick) if side == "LONG" else floor(raw, tick)
    if price <= 0 or (price - reference) * sign * (1 if favorable else -1) <= 0:
        raise ValueError("价格距离无法满足合约最小价格/步长")
    return price


class HedgeExecutor(Executor):
    def __init__(self, *args):
        super().__init__(*args)
        self.exchange.require_hedge = True
        self.hedge = HedgeEngine(self)

    async def protect(self, tid, manual_kind=None):
        t = self.store.trade(tid)
        if not t or t["status"] != "OPEN":
            return True
        d = json.loads(t["details"])
        if d.get("hedge_child"):
            return True
        try:
            g = self.hedge.get(tid)
            if g is None:
                if t["position_idx"] not in (1, 2):
                    raise ValueError("对冲系统禁止单向持仓模式")
                g = dict(
                    group_id=tid,
                    symbol=t["symbol"],
                    active=tid,
                    child=None,
                    generation=0,
                    phase="ARMING",
                    members=[tid],
                )
                self.hedge.save(g)
            if g["phase"] == "ARMING":
                await self.hedge.arm(g, manual=manual_kind is not None)
            return True
        except Exception as error:
            self.alert("HEDGE:" + tid, "monitor", {"trade_id": tid}, error)
            return False


class HedgeEngine:
    def __init__(self, executor):
        self.executor = executor
        self.store, self.exchange = executor.store, executor.exchange

    def get(self, gid):
        rows = self.store.rows("SELECT document FROM hedge_groups WHERE group_id=?", (gid,))
        return json.loads(rows[0]["document"]) if rows else None

    def groups(self):
        return [
            json.loads(r["document"]) for r in self.store.rows("SELECT document FROM hedge_groups")
        ]

    def save(self, g):
        self.store.execute(
            "INSERT INTO hedge_groups VALUES (?,?) ON CONFLICT(group_id) DO UPDATE SET document=excluded.document",
            (g["group_id"], encode(g)),
        )

    def blocked(self, tid):
        # During cancellation/reconciliation, legacy settlement must not race cleanup.
        t = self.store.trade(tid)
        if t and t["status"] == "INTENT" and json.loads(t["details"]).get("hedge_child"):
            return True
        return any(
            tid in g["members"] and g["phase"] not in ("SINGLE", "LOCKED", "DONE")
            for g in self.groups()
        )

    def locked(self):
        return [g for g in self.groups() if g["phase"] == "LOCKED"]

    async def slot(self, t):
        positions = await self.exchange.positions(t["symbol"])
        rows = [
            p
            for p in positions
            if int(p["positionIdx"]) == t["position_idx"] and number(p["size"]) > 0
        ]
        if not rows:
            return None
        if len(rows) != 1 or not matches_position(t, rows[0]):
            raise ValueError("对冲仓位身份或数量变化，停止自动修改并等待核对")
        return rows[0]

    async def intent(self, tid, kind, key, payload, *, manual=False):
        link = "h2-" + identity(tid, kind, key)
        payload = dict(payload, category="linear", orderLinkId=link)
        self.store.execute(
            "INSERT OR IGNORE INTO orders(link_id,trade_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (link, tid, kind, encode(payload), time.time()),
        )
        row = self.store.rows("SELECT * FROM orders WHERE link_id=?", (link,))[0]
        actual = await self.exchange.order(payload["symbol"], link)
        if actual:
            self.record(actual)
            if actual["orderStatus"] in TERMINAL - {"Filled"}:
                raise RuntimeError("原委托已取消或拒绝，需要人工核对，禁止当成挂单成功")
            return actual
        # An ambiguous submission is NEVER resent. Definitive rejections have a durable budget.
        budget = row["attempts"] + 1 if manual else 2
        while row["attempts"] < budget and (row["attempts"] == 0 or row["status"] == "Rejected"):
            self.store.execute(
                "UPDATE orders SET attempts=attempts+1,status='SUBMITTING' WHERE link_id=?", (link,)
            )
            try:
                oid = await self.exchange.submit(json.loads(row["payload"]))
                self.store.execute("UPDATE orders SET exchange_id=? WHERE link_id=?", (oid, link))
            except BybitAPIError as error:
                if (
                    error.definitive_rejection
                    and error.code > 0
                    and error.code not in {10000, 10014, 10016, 10019, 110072}
                ):
                    self.store.execute(
                        "UPDATE orders SET status='Rejected',receipt=? WHERE link_id=?",
                        (encode({"retCode": error.code}), link),
                    )
                else:
                    raise
            actual = await self.exchange.order(payload["symbol"], link)
            if actual:
                self.record(actual)
                if actual["orderStatus"] in TERMINAL - {"Filled"}:
                    raise RuntimeError("原委托已取消或拒绝，需要人工核对")
                return actual
            row = self.store.rows("SELECT * FROM orders WHERE link_id=?", (link,))[0]
        raise RuntimeError("对冲订单未确认或已拒绝；按原订单ID核对，不重复提交")

    def record(self, order):
        rows = self.store.rows(
            "SELECT payload FROM orders WHERE link_id=?", (order["orderLinkId"],)
        )
        if not rows:
            raise ValueError("无法确认订单归属")
        payload = json.loads(rows[0]["payload"])
        if (
            any(order.get(k) != payload[k] for k in ("symbol", "side", "reduceOnly", "orderType"))
            or int(order.get("positionIdx", -1)) != payload["positionIdx"]
        ):
            raise ValueError("对冲订单身份/方向/减仓标志不匹配")
        if number(order["qty"]) != number(payload["qty"]):
            raise ValueError("对冲订单数量被修改")
        if payload["orderType"] == "Limit" and number(order["price"]) != number(payload["price"]):
            raise ValueError("对冲或止盈限价被修改")
        self.store.execute(
            "UPDATE orders SET exchange_id=?,status=?,receipt=? WHERE link_id=?",
            (order["orderId"], order["orderStatus"], encode(order), order["orderLinkId"]),
        )

    async def cancel_link(self, symbol, link):
        order = await self.exchange.order(symbol, link)
        if order is None:
            raise RuntimeError("待撤订单状态不明，保持当前阶段等待核对")
        self.record(order)
        if order["orderStatus"] in LIVE:
            await self.exchange.cancel(symbol, order["orderId"])
            order = await self.exchange.order(symbol, link)
        if not order or order["orderStatus"] not in TERMINAL:
            raise RuntimeError("等待交易所确认撤单终态")
        self.record(order)
        return order

    async def cancel_exits(self, g):
        for tid in (g["active"], g.get("child")):
            if not tid:
                continue
            for r in self.store.rows(
                "SELECT link_id FROM orders WHERE trade_id=? AND kind IN ('TP','SL')", (tid,)
            ):
                await self.cancel_link(g["symbol"], r["link_id"])

    async def arm(self, g, *, manual=False):
        t = self.store.trade(g["active"])
        p = await self.slot(t)
        if not p:
            g["phase"] = "CLEANUP"
            self.save(g)
            return
        d = json.loads(t["details"])
        if "reference" not in g:
            g["reference"] = t["entry_price"]
        qty = number(p["size"])
        tick = number(d["tick"])
        reference = number(g["reference"])
        tp = number(g["tp"]) if "tp" in g else number(t["tp_price"])
        hedge = distance_price(
            t["side"], reference, qty, number(d["sl_loss"]), tick, favorable=False
        )
        self.store.update_trade(t["trade_id"], tp_price=str(tp), sl_price=str(hedge))
        # The child and its link are persisted before placing either opening order.
        child_id = g.get("child") or identity(g["group_id"], "hedge", g["generation"])
        link = "h2-" + identity(child_id, "ENTRY", 0)
        if not self.store.trade(child_id):
            child_d = {
                k: d[k] for k in ("tick", "maker", "taker", "target", "sl_loss", "entry_defaults")
            }
            child_d.update(
                entry_link=link,
                intent_at=time.time(),
                entry_fee=None,
                hedge_child=True,
                hedge_group=g["group_id"],
            )
            self.store.insert_trade(
                dict(
                    trade_id=child_id,
                    cycle_id=t["cycle_id"],
                    signal_id=None,
                    symbol=t["symbol"],
                    side="SHORT" if t["side"] == "LONG" else "LONG",
                    position_idx=3 - t["position_idx"],
                    status="INTENT",
                    qty=str(qty),
                    margin=str(qty * reference / number(t["leverage"])),
                    leverage=t["leverage"],
                    tp_target_net_pnl=t["tp_target_net_pnl"],
                    analysis=t["analysis"],
                    details=encode(child_d),
                )
            )
        g["child"] = child_id
        if child_id not in g["members"]:
            g["members"].append(child_id)
        self.save(g)
        payload = dict(
            symbol=t["symbol"],
            side="Sell" if t["side"] == "LONG" else "Buy",
            qty=str(qty),
            positionIdx=3 - t["position_idx"],
            reduceOnly=False,
            orderType="Limit",
            timeInForce="GTC",
            price=str(hedge),
            triggerPrice=str(hedge),
            triggerBy="LastPrice",
            triggerDirection=2 if t["side"] == "LONG" else 1,
        )
        # If an unsent trigger is already crossed, leave an ordinary limit at the same
        # price. Never convert to market or chase the price.
        if not self.store.rows("SELECT link_id FROM orders WHERE link_id=?", (link,)):
            last = await self.exchange.last_price(t["symbol"])
            if (last - hedge) * (1 if t["side"] == "LONG" else -1) <= 0:
                for key in ("triggerPrice", "triggerBy", "triggerDirection"):
                    payload.pop(key)
        order = await self.intent(
            t["trade_id"],
            "TP",
            g["generation"],
            dict(
                symbol=t["symbol"],
                side="Sell" if t["side"] == "LONG" else "Buy",
                qty=str(qty),
                positionIdx=t["position_idx"],
                reduceOnly=True,
                orderType="Limit",
                timeInForce="GTC",
                price=str(tp),
            ),
            manual=manual,
        )
        self.store.update_trade(t["trade_id"], tp_order_id=order["orderId"])
        # Protect the surviving direction before submitting the opposite opening.
        # A TP may fill during its acknowledgement; never open a fresh hedge for
        # a position that has already exited.
        if await self.slot(t) is None:
            g["phase"] = "CLEANUP"
            self.save(g)
            await self.advance(g)
            return
        await self.intent(child_id, "ENTRY", 0, payload, manual=manual)
        g["phase"] = "SINGLE"
        self.save(g)
        self.store.queue(
            f"armed:{g['group_id']}:{g['generation']}",
            f"✅ {'开仓' if g['generation'] == 0 else '重新判向'} · {t['symbol']} · {'做多' if t['side'] == 'LONG' else '做空'}\n"
            f"数量 {qty} · 基准 {reference}\n止盈 {tp} · 对冲触发/限价 {hedge}\n对冲部分成交保留止盈；全部成交后等待判向",
        )

    def defer_notice(self, g, order):
        pending = g.setdefault("notice_pending", {})
        key = order["orderLinkId"]
        task = pending.setdefault(key, {"generation": g["generation"]})
        task["order"] = order
        if "partial_at" in g and task["generation"] == g["generation"]:
            task["partial_at"] = g["partial_at"]
        self.save(g)

    async def notice_fill(self, g, order):
        filled = number(order.get("cumExecQty") or 0)
        if filled <= 0:
            return
        rows = []
        if "partial_at" not in g or order["orderStatus"] == "Filled":
            rows = await self.exchange.executions(g["symbol"], order["orderId"])
            rows = [
                r
                for r in rows
                if r.get("execType") == "Trade" and r.get("orderId") == order["orderId"]
            ]
            if not rows:
                raise RuntimeError("成交通知等待交易所逐笔成交时间，不阻塞撤止盈与仓位处理")
            g["partial_at"] = min(
                g.get("partial_at", float("inf")), min(int(r["execTime"]) / 1000 for r in rows)
            )
        key = f"hedge:{g['group_id']}:{g['generation']}"
        if order["orderStatus"] == "Filled":
            if sum((number(r["execQty"]) for r in rows), D(0)) != filled:
                raise RuntimeError("全成通知等待逐笔成交数量齐全，交易处理继续")
            completed = max(int(r["execTime"]) / 1000 for r in rows)
            if completed - g["partial_at"] > 30:
                at_deadline = sum(
                    (
                        number(r["execQty"])
                        for r in rows
                        if int(r["execTime"]) / 1000 <= g["partial_at"] + 30
                    ),
                    D(0),
                )
                self.store.queue(
                    key + ":partial",
                    f"⏳ 对冲部分成交记录 · {g['symbol']}\n首次成交30秒时已成交 {at_deadline} · 剩余 {number(order['qty']) - at_deadline}\n该订单随后已全部成交",
                )
            self.store.queue(
                key + ":full", f"✅ 对冲全部成交 · {g['symbol']}\n已成交 {filled} · 剩余 0"
            )
        elif time.time() - g["partial_at"] >= 30:
            self.store.queue(
                key + ":partial",
                f"⏳ 对冲部分成交 · {g['symbol']}\n已成交 {filled} · 剩余 {number(order['qty']) - filled}\n剩余限价单继续等待成交",
            )

    async def close_leg(self, t, kind, key):
        # Confirm every previous request terminal before submitting a remaining-quantity exit.
        rows = self.store.rows(
            "SELECT * FROM orders WHERE trade_id=? AND kind=? ORDER BY created_at",
            (t["trade_id"], kind),
        )
        for row in rows:
            o = await self.exchange.order(t["symbol"], row["link_id"])
            if not o and row["attempts"] == 0:
                payload = json.loads(row["payload"])
                self.store.execute(
                    "UPDATE orders SET attempts=1,status='SUBMITTING' WHERE link_id=?",
                    (row["link_id"],),
                )
                await self.exchange.submit(payload)
                o = await self.exchange.order(t["symbol"], row["link_id"])
            if not o or o["orderStatus"] not in TERMINAL:
                raise RuntimeError("平仓订单尚未确认，禁止重复平仓")
            self.record(o)
        p = await self.slot(t)
        if not p:
            return True
        if len(rows) >= 2:
            raise RuntimeError("平仓两次提交后仍有余仓，请人工核对")
        o = await self.intent(
            t["trade_id"],
            kind,
            f"{key}:{len(rows)}",
            dict(
                symbol=t["symbol"],
                side="Sell" if t["side"] == "LONG" else "Buy",
                qty=str(p["size"]),
                positionIdx=t["position_idx"],
                reduceOnly=True,
                closeOnTrigger=True,
                orderType="Market",
                timeInForce="IOC",
            ),
        )
        return o["orderStatus"] in TERMINAL and await self.slot(t) is None

    async def advance(self, g):
        t = self.store.trade(g["active"])
        if g["phase"] == "ARMING":
            await self.arm(g)
            return
        child = self.store.trade(g["child"]) if g.get("child") else None
        if g["phase"] in ("SINGLE", "LOCKING", "LOCKED") and child is None:
            raise ValueError("对冲子仓记录缺失")
        if g["phase"] == "SINGLE":
            assert child is not None
            link = json.loads(child["details"])["entry_link"]
            order = await self.exchange.order(g["symbol"], link)
            if not order:
                raise RuntimeError("对冲委托无法确认")
            self.record(order)
            self.defer_notice(g, order)
            if await self.slot(t) is None:
                g["phase"] = "CLEANUP"
            elif order["orderStatus"] == "Filled":
                g["phase"] = "LOCKING"
            elif order["orderStatus"] in TERMINAL:
                raise RuntimeError("对冲委托取消或拒绝，请核对；不会自动重挂")
            else:
                # Missing TP is an incident, never silently ignored.
                tp_rows = self.store.rows(
                    "SELECT link_id FROM orders WHERE trade_id=? AND kind='TP' ORDER BY created_at DESC",
                    (t["trade_id"],),
                )
                tp = (
                    await self.exchange.order(g["symbol"], tp_rows[0]["link_id"])
                    if tp_rows
                    else None
                )
                if not tp or tp["orderStatus"] not in LIVE | {"Filled"}:
                    raise RuntimeError("顺向止盈单缺失或取消，请核对")
            self.save(g)
        if g["phase"] == "LOCKING":
            assert child is not None
            await self.cancel_exits(g)
            await self.executor.reconcile_entry(child["trade_id"])
            child = self.store.trade(child["trade_id"])
            if child["status"] == "INTENT":
                return
            if await self.slot(t) is None:
                g["phase"] = "CLEANUP"
            else:
                g["phase"] = "LOCKED"
            self.save(g)
        if g["phase"] == "LOCKED":
            if await self.slot(t) is None or await self.slot(child) is None:
                raise RuntimeError("等待判向的双向仓位发生外部变化，请人工核对")
        if g["phase"] == "CLEANUP":
            if child:
                child_link = json.loads(child["details"])["entry_link"]
                if not self.store.rows("SELECT link_id FROM orders WHERE link_id=?", (child_link,)):
                    # A child ledger row precedes TP submission, but no order intent
                    # means its opening request has never been sent.
                    if child["status"] != "INTENT" or await self.slot(child) is not None:
                        raise ValueError("未提交对冲意图却存在仓位或非预期账本状态")
                    self.store.update_trade(child["trade_id"], status="UNFILLED")
                else:
                    order = await self.cancel_link(g["symbol"], child_link)
                    self.defer_notice(g, order)
                    await self.executor.reconcile_entry(child["trade_id"])
                child = self.store.trade(child["trade_id"])
                if child["status"] == "INTENT":
                    return
                if child["status"] != "UNFILLED" and not await self.close_leg(
                    child, "HEDGE_CLEANUP", g["generation"]
                ):
                    return
            await self.cancel_exits(g)
            g["phase"] = "DONE"
            self.save(g)
        if g["phase"] == "UNLOCKING":
            loser = self.store.trade(g["loser"])
            if not await self.close_leg(loser, "HEDGE_CLOSE", g["generation"]):
                return
            # Accounting may lag; protect the surviving leg immediately. The old
            # leg remains SETTLING and is reconciled by its own order identities.
            self.store.update_trade(loser["trade_id"], status="SETTLING")
            g.update(active=g["winner"], child=None, generation=g["generation"] + 1, phase="ARMING")
            g.pop("partial_at", None)
            self.save(g)
            await self.arm(g)

    async def tick(self):
        if not self.executor.settings.trading_enabled:
            return
        known = {tid for g in self.groups() for tid in g["members"]}
        for t in self.store.rows("SELECT * FROM trades WHERE owned=1 AND status='OPEN'"):
            if t["trade_id"] not in known and not json.loads(t["details"]).get("hedge_child"):
                await self.executor.protect(t["trade_id"])
        for g in self.groups():
            if g["phase"] == "DONE" and not g.get("notice_pending"):
                continue
            try:
                if g["phase"] != "DONE":
                    await self.advance(g)
                self.store.resolve("HEDGE:" + g["group_id"])
            except Exception as error:
                self.executor.alert(
                    "HEDGE:" + g["group_id"], "monitor", {"trade_id": g["group_id"]}, error
                )

            # Notification history is independent of urgent cancellation/cleanup.
            current = self.get(g["group_id"])
            for link, task in list(current.get("notice_pending", {}).items()):
                view = dict(current, generation=task["generation"])
                view.pop("partial_at", None)
                if "partial_at" in task:
                    view["partial_at"] = task["partial_at"]
                try:
                    await self.notice_fill(view, task["order"])
                    current["notice_pending"].pop(link)
                except Exception as error:
                    self.executor.alert(
                        "HEDGE_NOTICE:" + g["group_id"],
                        "monitor",
                        {"trade_id": g["group_id"]},
                        error,
                    )
                if "partial_at" in view:
                    task["partial_at"] = view["partial_at"]
                    if task["generation"] == current["generation"]:
                        current["partial_at"] = view["partial_at"]
                self.save(current)
            if not current.get("notice_pending"):
                self.store.resolve("HEDGE_NOTICE:" + g["group_id"])

    async def decide(self, gid, sid, decision, *, deadline=None, expected_generation=None):
        async with self.executor.lock:
            g = self.get(gid)
            if not g or g["phase"] != "LOCKED" or decision.decision == "SKIP":
                return "SKIP_MODEL"
            if expected_generation is not None and g["generation"] != expected_generation:
                return "SKIP_HEDGE_GENERATION"
            a, b = self.store.trade(g["active"]), self.store.trade(g["child"])
            winner, loser = (a, b) if a["side"] == decision.decision else (b, a)
            p = await self.slot(winner)
            if p is None or await self.slot(loser) is None:
                raise ValueError("判向前双向仓位不完整")
            candles = await self.executor.markets.reachability(g["symbol"])
            instrument = await self.exchange.instrument(g["symbol"])
            bid, ask = await self.exchange.quote(g["symbol"])
            reference = ask if decision.decision == "LONG" else bid
            qty = number(p["size"])
            target = number(winner["tp_target_net_pnl"])
            tp = distance_price(
                decision.decision, reference, qty, target, instrument.tick, favorable=True
            )
            # Reuse the original 24h evidence rule against the EXACT proposed price.
            reachable = reachable_tp(
                decision.decision,
                reference,
                qty,
                instrument,
                D(0),
                D(0),
                ask - bid,
                candles,
                targets=(target,),
                exact_price=tp,
            )
            self.store.event(
                "HEDGE_TP_REACHABILITY",
                dict(group_id=gid, signal_id=sid, reference=reference, result=reachable),
            )
            if reachable is None:
                self.store.signal_result(sid, "SKIP_TP_UNREACHABLE", "SKIP")
                return "SKIP_TP_UNREACHABLE"
            if deadline is not None and time.time() >= deadline:
                return "SKIP_CYCLE_ENDED"
            if not self.executor.settings.trading_enabled:
                return "DRY_RUN_ELIGIBLE"
            g.update(
                phase="UNLOCKING",
                winner=winner["trade_id"],
                loser=loser["trade_id"],
                reference=str(reference),
                tp=str(tp),
                signal_id=sid,
            )
            self.save(g)
            await self.advance(g)
            return "HEDGE_DIRECTION_APPLIED"
