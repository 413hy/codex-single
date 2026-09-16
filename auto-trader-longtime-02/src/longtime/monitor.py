from __future__ import annotations

import json
import time
from decimal import Decimal as D

from longtime.exchange import LIVE
from longtime.execution import matches_position
from longtime.risk import number
from longtime.store import encode

SETTLEMENT_NOTICE_GRACE_SECONDS = 300


class Monitor:
    def __init__(self, executor):
        self.executor = executor
        self.store, self.exchange = executor.store, executor.exchange

    async def capture_entry_evidence(self, tid):
        t = self.store.trade(tid)
        d = json.loads(t["details"])
        if not d.get("entry_evidence_pending"):
            return
        rows = await self.exchange.executions(t["symbol"], t["order_id"], at=d["intent_at"])
        rows = [
            r for r in rows if r.get("execType") == "Trade" and r.get("orderId") == t["order_id"]
        ]
        if sum((number(r["execQty"]) for r in rows), D(0)) != number(t["qty"]):
            return
        if sum((number(r["execFee"]) for r in rows), D(0)) != number(d["entry_fee"]):
            raise ValueError("Entry execution fee differs from confirmed order aggregate")
        opened = min(int(r["execTime"]) for r in rows) / 1000
        d.update(entry_executions=rows, entry_evidence_pending=False, opened_at_source="executions")
        self.store.update_trade(tid, details=encode(d), opened_at=opened)

    async def startup(self):
        await self.exchange.sync_clock()
        account = await self.exchange.account()
        positions = await self.exchange.active_positions()
        orders = await self.exchange.open_orders()
        self.store.event(
            "STARTUP_EXCHANGE_STATE", {"account": account, "positions": positions, "orders": orders}
        )
        self.store.execute("UPDATE incidents SET status='OPEN' WHERE status='RUNNING'")
        self.store.execute(
            "UPDATE cycles SET status='INTERRUPTED',completed_at=? WHERE status='RUNNING'",
            (time.time(),),
        )
        await self.tick()

    async def tick(self):
        if hasattr(self.executor, "hedge"):
            async with self.executor.lock:
                await self.executor.hedge.tick()
        positions = await self.exchange.active_positions()
        self.store.set("exchange_positions", positions)
        own = self.store.rows(
            "SELECT * FROM trades WHERE owned=1 AND status IN ('INTENT','OPEN','SETTLING')"
        )
        for t in own:
            tid = t["trade_id"]
            try:
                async with self.executor.lock:
                    if hasattr(self.executor, "hedge") and self.executor.hedge.blocked(tid):
                        continue
                    if t["status"] == "INTENT":
                        if not await self.executor.reconcile_entry(tid):
                            self.executor.alert(
                                "ENTRY:" + tid,
                                "reconcile",
                                {"trade_id": tid},
                                RuntimeError("成交状态尚未确认；禁止重复开仓"),
                            )
                        continue
                    if t["status"] == "SETTLING":
                        await self.settle(tid)
                        continue
                    current = [
                        p
                        for p in await self.exchange.positions(t["symbol"])
                        if number(p["size"]) > 0 and int(p["positionIdx"]) == t["position_idx"]
                    ]
                    if not current:
                        d = json.loads(t["details"])
                        d["flat_detected_at"] = time.time()
                        self.store.update_trade(tid, status="SETTLING", details=encode(d))
                        await self.settle(tid)
                        continue
                    if not any(matches_position(t, p) for p in current):
                        raise ValueError("持仓身份/数量变化；只报警，不修改仓位")
                    if number(current[0]["size"]) < number(t["qty"]):
                        # Archive partial exit evidence while still available in Demo history.
                        # This is accounting only; no position or protection changes.
                        await self.capture_settlement(t)
                    # Observe presence only. Missing protection does not cause automatic replacement.
                    for kind in (() if hasattr(self.executor, "hedge") else ("SL", "TP")):
                        rows = self.store.rows(
                            "SELECT * FROM orders WHERE trade_id=? AND kind=? ORDER BY created_at DESC",
                            (tid, kind),
                        )
                        order = (
                            await self.exchange.order(t["symbol"], rows[0]["link_id"])
                            if rows
                            else None
                        )
                        if order and order["orderStatus"] in LIVE:
                            self.executor.verify_protection(
                                t, kind, order, number(current[0]["size"])
                            )
                            self.executor.record_protection(tid, kind, order)
                        else:
                            # Exit orders can settle between the position and order reads.
                            # Recheck before claiming that an open position lacks protection.
                            latest = [
                                p
                                for p in await self.exchange.positions(t["symbol"])
                                if number(p["size"]) > 0
                                and int(p["positionIdx"]) == t["position_idx"]
                            ]
                            if not latest:
                                break
                            previous = self.store.rows(
                                "SELECT error FROM incidents WHERE scope=? AND status='OPEN'",
                                (kind + ":" + tid,),
                            )
                            diagnostic = (
                                previous[0]["error"]
                                if previous
                                else f"{kind}缺失/已结束，点击重试核对并补挂原始保护单"
                            )
                            self.executor.alert(
                                kind + ":" + tid,
                                "protection",
                                {"trade_id": tid, "kind": kind},
                                RuntimeError(diagnostic),
                            )
                    await self.capture_entry_evidence(tid)
                self.store.resolve("POSITION:" + tid)
            except Exception as error:
                self.executor.alert("POSITION:" + tid, "monitor", {"trade_id": tid}, error)
        # Exchange positions unknown to our ledger remain observed, never adopted/modified.
        known = {
            (t["symbol"], t["position_idx"])
            for t in self.store.rows(
                "SELECT symbol,position_idx FROM trades WHERE owned=1 AND status IN ('INTENT','OPEN','SETTLING')"
            )
        }
        positions = await self.exchange.active_positions()
        self.store.set("exchange_positions", positions)
        observed = {
            (p["symbol"], int(p["positionIdx"])): p
            for p in positions
            if (p["symbol"], int(p["positionIdx"])) not in known
        }
        previous = self.store.state("external_positions", {})
        fresh = {}
        for (symbol, idx), p in observed.items():
            key = f"{symbol}:{idx}"
            fresh[key] = previous.get(key, {"first_observed_at": time.time()}) | {"position": p}
            if key not in previous:
                self.store.event("EXTERNAL_POSITION_OBSERVED", fresh[key])
        for symbol in sorted({symbol for symbol, _ in observed}):
            await self.check_external(symbol)
        for key in previous.keys() - fresh.keys():
            self.store.event(
                "EXTERNAL_POSITION_ENDED",
                {
                    **previous[key],
                    "detected_at": time.time(),
                    "accounting": "EXTERNAL_LEDGER_REQUIRED",
                },
            )
            self.store.resolve("EXTERNAL:" + key)
        self.store.set("external_positions", fresh)
        self.store.set("monitor_heartbeat", time.time())
        self.store.resolve("MONITOR")

    async def check_external(self, symbol):
        owned_slots = {
            (t["symbol"], t["position_idx"])
            for t in self.store.rows(
                "SELECT symbol,position_idx FROM trades WHERE owned=1 AND status IN ('INTENT','OPEN','SETTLING')"
            )
        }
        positions = [
            p
            for p in await self.exchange.positions(symbol)
            if number(p["size"]) > 0 and (p["symbol"], int(p["positionIdx"])) not in owned_slots
        ]
        orders = await self.exchange.open_orders(symbol)
        ok = True
        for p in positions:
            key = f"EXTERNAL:{symbol}:{p['positionIdx']}"
            side = "Sell" if p["side"] == "Buy" else "Buy"
            exits = [
                o
                for o in orders
                if o.get("reduceOnly") is True
                and o.get("orderStatus") in LIVE
                and number(o.get("leavesQty") or o.get("qty") or 0) >= number(p["size"])
                and o.get("side") == side
                and int(o.get("positionIdx", -1)) == int(p["positionIdx"])
            ]
            has_tp = number(p.get("takeProfit") or 0) > 0 or any(
                o.get("orderType") == "Limit"
                and (number(o.get("price") or 0) - number(p["avgPrice"]))
                * (1 if p["side"] == "Buy" else -1)
                > 0
                for o in exits
            )
            has_sl = number(p.get("stopLoss") or 0) > 0 or any(
                o.get("orderType") == "Market" and o.get("triggerPrice") not in (None, "", "0")
                for o in exits
            )
            if has_tp and has_sl:
                self.store.resolve(key)
            else:
                ok = False
                self.executor.alert(
                    key,
                    "monitor",
                    {"symbol": symbol},
                    RuntimeError("外部已有仓位缺少可确认保护单；重试仅重新核对，不接管或改价"),
                )
        return ok

    async def cleanup(self, t):
        # Only cancel this lifecycle's remaining orders after exchange confirms this slot flat.
        active = [
            p
            for p in await self.exchange.positions(t["symbol"])
            if int(p["positionIdx"]) == t["position_idx"] and number(p["size"]) > 0
        ]
        if active:
            replacement = self.store.rows(
                "SELECT * FROM trades WHERE symbol=? AND position_idx=? AND trade_id!=? AND status='OPEN'",
                (t["symbol"], t["position_idx"], t["trade_id"]),
            )
            if not (hasattr(self.executor, "hedge") and replacement
                    and any(matches_position(other, p) for other in replacement for p in active)):
                raise ValueError("Cleanup refused: position slot is no longer flat")
        links = {
            r["link_id"]
            for r in self.store.rows(
                "SELECT link_id FROM orders WHERE trade_id=? AND kind IN ('SL','TP')",
                (t["trade_id"],),
            )
        }
        orders = await self.exchange.open_orders(t["symbol"])
        for order in orders:
            if order.get("orderLinkId") in links:
                await self.exchange.cancel(t["symbol"], order["orderId"])
        remaining = await self.exchange.open_orders(t["symbol"])
        if any(o.get("orderLinkId") in links for o in remaining):
            raise RuntimeError("Waiting for sibling protection cancellation confirmation")

    async def capture_settlement(self, t):
        d = json.loads(t["details"])
        now = time.time()
        # History filters can use order creation time, not the later fill time.
        # A sliding cursor loses old TP/SL orders after delayed publication.
        # Re-read the lifecycle window and merge by IDs with archived evidence.
        end = int(now * 1000)
        start = int((t["opened_at"] - 1) * 1000)
        orders = await self.exchange.history_window("/v5/order/history", t["symbol"], start, end)
        pnl = await self.exchange.history_window("/v5/position/closed-pnl", t["symbol"], start, end)
        known_orders = d.get("settlement_orders", {})
        known_pnl = d.get("settlement_pnl", {})
        known_execs = d.get("settlement_executions", {})
        known_orders.update({o["orderId"]: o for o in orders})
        known_pnl.update({r["orderId"]: r for r in pnl})
        closing_side = "Sell" if t["side"] == "LONG" else "Buy"
        for row in pnl:
            order = known_orders.get(row["orderId"])
            if (
                not order
                or order.get("side") != closing_side
                or int(order.get("positionIdx", -1)) != t["position_idx"]
            ):
                continue
            executions = await self.exchange.executions(
                t["symbol"], row["orderId"], at=int(row["updatedTime"]) / 1000
            )
            for e in executions:
                if e.get("orderId") == row["orderId"] and e.get("execType") in (
                    "Trade",
                    "BustTrade",
                ):
                    eid = e.get("execId") or encode(
                        [e["orderId"], e["execTime"], e["execQty"], e["execFee"]]
                    )
                    known_execs[eid] = e
        d.update(
            settlement_orders=known_orders,
            settlement_pnl=known_pnl,
            settlement_executions=known_execs,
            settlement_cursor=end / 1000,
        )
        self.store.update_trade(t["trade_id"], details=encode(d))
        return d

    def settlement_pending(self, t, details, now, reason):
        first_seen = details.setdefault("flat_detected_at", now)
        details["settlement_pending_reason"] = reason
        self.store.update_trade(t["trade_id"], details=encode(details))
        if now - first_seen >= SETTLEMENT_NOTICE_GRACE_SECONDS:
            self.executor.alert(
                "SETTLEMENT:" + t["trade_id"],
                "monitor",
                {"trade_id": t["trade_id"]},
                RuntimeError("仓位已结束，等待Bybit实际成交与盈亏记录匹配；" + reason),
            )

    async def settle(self, tid):
        await self.capture_entry_evidence(tid)
        t = self.store.trade(tid)
        if t["status"] != "SETTLING":
            return True
        await self.cleanup(t)
        d = json.loads(t["details"])
        d = await self.capture_settlement(t)
        now = time.time()
        known_orders = d.get("settlement_orders", {})
        known_pnl = d.get("settlement_pnl", {})
        closing_side = "Sell" if t["side"] == "LONG" else "Buy"
        matches = []
        own_exit_ids = {r["exchange_id"] for r in self.store.rows(
            "SELECT exchange_id FROM orders WHERE trade_id=? AND kind!='ENTRY'", (tid,)
        )}
        for oid, row in known_pnl.items():
            if hasattr(self.executor, "hedge") and oid not in own_exit_ids:
                continue
            order = known_orders.get(oid)
            if (
                not order
                or order.get("side") != closing_side
                or int(order.get("positionIdx", -1)) != t["position_idx"]
            ):
                continue
            if (
                row.get("symbol") != t["symbol"]
                or row.get("side") != closing_side
                or row.get("execType") not in ("Trade", "BustTrade")
                or int(row["updatedTime"]) / 1000 < t["opened_at"]
            ):
                continue
            qty = number(row["closedSize"])
            if qty <= 0 or qty != number(order.get("cumExecQty") or 0):
                continue
            if abs(number(row["avgEntryPrice"]) - number(t["entry_price"])) > number(
                t["entry_price"]
            ) * D("0.000001"):
                continue
            matches.append((row, order))
        if sum((number(r["closedSize"]) for r, _ in matches), D(0)) != number(t["qty"]):
            self.settlement_pending(t, d, now, "平仓订单/盈亏数量尚未匹配")
            return False
        exit_value, net, fees, closed_at = D(0), D(0), D(0), 0.0
        reasons, exit_executions = [], []
        own_ids = {
            r["exchange_id"]: r["kind"]
            for r in self.store.rows("SELECT exchange_id,kind FROM orders WHERE trade_id=?", (tid,))
        }
        for row, order in matches:
            executions = [
                e
                for e in d.get("settlement_executions", {}).values()
                if e.get("orderId") == row["orderId"]
            ]
            if sum((number(e["execQty"]) for e in executions), D(0)) != number(row["closedSize"]):
                self.settlement_pending(t, d, now, "平仓逐笔成交数量尚未齐全")
                return False
            exit_executions.extend(executions)
            closed_at = max(closed_at, max(int(e["execTime"]) / 1000 for e in executions))
            exit_value += number(row["avgExitPrice"]) * number(row["closedSize"])
            net += number(row["closedPnl"])
            fees += sum((number(e["execFee"]) for e in executions), D(0))
            reason = own_ids.get(row["orderId"], "OTHER")
            if "Liq" in order.get("createType", ""):
                reason = "LIQUIDATION"
            reasons.append(reason)
        fees += number(d["entry_fee"])
        exit_price = exit_value / number(t["qty"])
        gross = (
            (exit_price - number(t["entry_price"]))
            * number(t["qty"])
            * (1 if t["side"] == "LONG" else -1)
        )
        reason = reasons[0] if len(set(reasons)) == 1 else "MIXED"
        d.update(
            exit_executions=exit_executions,
            exchange_closed_pnl=str(net),
            other_pnl_adjustment=str(net - (gross - fees)),
        )
        close_label = {
            "TP": "止盈",
            "SL": "止损",
            "LIQUIDATION": "强平",
            "OTHER": "其他平仓",
            "MIXED": "分笔平仓",
            "HEDGE_CLOSE": "判向平仓",
            "HEDGE_CLEANUP": "对冲余仓平仓",
        }[reason]
        self.store.update_trade(
            tid,
            notification=(
                "closed:" + tid,
                f"💰 {close_label} · {t['symbol']} · {'做多' if t['side'] == 'LONG' else '做空'}\n"
                f"净收益 {net:+.4f}U\n"
                f"开仓 {t['entry_price']} → 平仓 {exit_price}\n"
                f"持仓 {(closed_at - t['opened_at']) / 60:.1f}分钟",
            ),
            status="CLOSED",
            exit_price=str(exit_price),
            closed_at=closed_at,
            holding_duration=closed_at - t["opened_at"],
            realized_pnl=str(gross),
            fees=str(fees),
            net_pnl=str(net),
            close_reason=reason,
            details=encode(d),
        )
        for scope in ("SL", "TP", "ENTRY", "POSITION", "SETTLEMENT"):
            self.store.resolve(scope + ":" + tid)
        return True
