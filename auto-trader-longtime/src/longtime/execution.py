from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from decimal import Decimal as D

from longtime import emergency
from longtime.exchange import LIVE, TERMINAL
from longtime.risk import RESERVE, SLIPPAGE, number, reachable_tp, sl_price, tp_price
from longtime.store import encode, identity
from longtime.trading_settings import EntryDefaults
from longtime.transport import BybitAPIError

log = logging.getLogger(__name__)


def matches_position(trade, p):
    return (
        p["symbol"] == trade["symbol"]
        and int(p["positionIdx"]) == trade["position_idx"]
        and p.get("side") == ("Buy" if trade["side"] == "LONG" else "Sell")
        and number(p["size"]) > 0
        and abs(number(p["avgPrice"]) - number(trade["entry_price"]))
        <= number(trade["entry_price"]) * D("0.000001")
        and number(p["size"]) <= number(trade["qty"])
    )


class Executor:
    def __init__(self, settings, store, exchange, markets):
        self.settings, self.store, self.exchange, self.markets = settings, store, exchange, markets
        self.lock = asyncio.Lock()

    def alert(self, scope, kind, payload, error):
        # Never format HTTP exceptions containing bot credentials.
        text = (
            str(error)[:600]
            if isinstance(error, (ValueError, RuntimeError))
            else type(error).__name__
        )
        try:
            existing = self.store.rows(
                "SELECT incident_id,error FROM incidents WHERE scope=? AND status IN ('OPEN','RUNNING')",
                (scope,),
            )
            if existing and existing[0]["error"] == text:
                return existing[0]["incident_id"]
            log.error("%s: %s", scope, text)
            self.store.event("ERROR", {"scope": scope, "error": text})
            return self.store.incident(scope, kind, payload, text)
        except sqlite3.Error:
            log.error("Database unavailable; using independent emergency notification file")
            return emergency.queue(self.settings.runtime_dir, scope, "数据库读写异常")

    async def enter(self, cycle_id, signal_id, decision, *, deadline=None):
        symbol, side = decision.symbol, decision.decision
        async with self.lock:
            positions = await self.exchange.active_positions()
            if symbol in {p["symbol"] for p in positions} | self.store.reserved():
                self.store.signal_result(signal_id, "SKIP_EXISTING_POSITION")
                return "SKIP_EXISTING_POSITION"
            instrument = await self.exchange.instrument(symbol)
            taker, maker = await self.exchange.fees(symbol)
            candles = await self.markets.reachability(symbol)
            bid, ask = await self.exchange.quote(symbol)
            price = ask if side == "LONG" else bid
            defaults = EntryDefaults.load(self.store)
            qty, leverage, margin = instrument.size(
                price, margin_target=defaults.margin, leverage_target=defaults.leverage
            )
            target = reachable_tp(
                side,
                price,
                qty,
                instrument,
                taker,
                maker,
                ask - bid,
                candles,
                targets=(defaults.tp,),
            )
            self.store.event(
                "TP_REACHABILITY",
                {
                    "signal_id": signal_id,
                    "target": target,
                    "quote": [str(bid), str(ask)],
                    "candles": [c.model_dump(mode="json") for c in candles],
                    "taker_fee_rate": str(taker),
                    "maker_fee_rate": str(maker),
                },
            )
            if target is None:
                self.store.signal_result(signal_id, "SKIP_TP_UNREACHABLE")
                return "SKIP_TP_UNREACHABLE"
            # Full account and positions re-read immediately before each new order.
            account = await self.exchange.account()
            positions = await self.exchange.active_positions()
            self.store.event(
                "PRE_ENTRY_ACCOUNT",
                {"signal_id": signal_id, "account": account, "positions": positions},
            )
            if symbol in {p["symbol"] for p in positions}:
                self.store.signal_result(signal_id, "SKIP_EXISTING_POSITION")
                return "SKIP_EXISTING_POSITION"
            orders = await self.exchange.open_orders(symbol)
            if orders:
                self.store.signal_result(signal_id, "SKIP_EXISTING_ORDERS")
                return "SKIP_EXISTING_ORDERS"
            # Reserve entry/exit taker fees and bounded execution slippage as well as margin.
            required = margin * D("1.005") + qty * price * (taker * 2 + SLIPPAGE)
            if number(account["available"]) - required < RESERVE:
                self.store.signal_result(signal_id, "STOP_INSUFFICIENT_MARGIN")
                return "STOP_INSUFFICIENT_MARGIN"
            if not self.settings.trading_enabled:
                self.store.signal_result(signal_id, "DRY_RUN_ELIGIBLE", side, decision.model_dump())
                return "DRY_RUN_ELIGIBLE"
            if self.store.state("entries_paused", False):
                self.store.signal_result(signal_id, "SKIP_PAUSED")
                return "SKIP_PAUSED"
            idx = await self.exchange.configure_symbol(symbol, side, leverage)
            # set-leverage can take time; recheck the wallet after configuration as well.
            account = await self.exchange.account()
            if number(account["available"]) - required < RESERVE:
                self.store.signal_result(signal_id, "STOP_INSUFFICIENT_MARGIN")
                return "STOP_INSUFFICIENT_MARGIN"
            # Pause can arrive while set-leverage/account requests are in flight.
            if self.store.state("entries_paused", False):
                self.store.signal_result(signal_id, "SKIP_PAUSED")
                return "SKIP_PAUSED"
            latest_positions = await self.exchange.active_positions()
            if symbol in {p["symbol"] for p in latest_positions}:
                self.store.signal_result(signal_id, "SKIP_EXISTING_POSITION")
                return "SKIP_EXISTING_POSITION"
            if deadline is not None and time.time() >= deadline:
                self.store.signal_result(signal_id, "SKIP_CYCLE_ENDED")
                return "SKIP_CYCLE_ENDED"
            if EntryDefaults.load(self.store) != defaults:
                self.store.signal_result(signal_id, "SKIP_SETTINGS_CHANGED")
                return "SKIP_SETTINGS_CHANGED"
            tid = identity("trade", signal_id)
            link = "lt-e-" + tid
            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy" if side == "LONG" else "Sell",
                "orderType": "Market",
                "qty": format(qty, "f"),
                "positionIdx": idx,
                "reduceOnly": False,
                "slippageToleranceType": "Percent",
                "slippageTolerance": "0.5",
                "orderLinkId": link,
            }
            details = {
                "entry_link": link,
                "tick": str(instrument.tick),
                "maker": str(maker),
                "taker": str(taker),
                "target": target,
                "sl_loss": str(defaults.sl),
                "risk_policy": "owner-entry-defaults-v1",
                "entry_defaults": defaults.document(),
                "intent_at": time.time(),
                "entry_fee": None,
            }
            self.store.insert_trade(
                {
                    "trade_id": tid,
                    "cycle_id": cycle_id,
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "side": side,
                    "position_idx": idx,
                    "status": "INTENT",
                    "analysis": encode(decision.model_dump()),
                    "qty": format(qty, "f"),
                    "margin": str(margin),
                    "leverage": str(leverage),
                    "tp_target_net_pnl": target["target"],
                    "details": encode(details),
                }
            )
            self.store.execute(
                "INSERT INTO orders(link_id,trade_id,kind,payload,created_at) VALUES (?,?,'ENTRY',?,?)",
                (link, tid, encode(payload), time.time()),
            )
            self.store.execute(
                "UPDATE orders SET attempts=1,status='SUBMITTING' WHERE link_id=?", (link,)
            )
            try:
                oid = await self.exchange.submit(payload)
                self.store.execute("UPDATE orders SET exchange_id=? WHERE link_id=?", (oid, link))
            except Exception as error:
                if (
                    isinstance(error, BybitAPIError)
                    and error.definitive_rejection
                    and error.code > 0
                    and error.code not in {10000, 10014, 10016, 10019, 110072}
                ):
                    self.store.update_trade(tid, status="UNFILLED")
                    self.store.execute(
                        "UPDATE orders SET status='REJECTED' WHERE link_id=?", (link,)
                    )
                    self.store.signal_result(signal_id, "ERROR")
                    self.alert("ENTRY:" + tid, "candidate", {"symbol": symbol}, error)
                    return "UNFILLED"
                # Never send a second opening request after an ambiguous acknowledgement.
                log.warning(
                    "Entry acknowledgement uncertain; reconciling original order: %s",
                    type(error).__name__,
                )
                self.store.event(
                    "ENTRY_ACK_UNCERTAIN", {"trade_id": tid, "error_type": type(error).__name__}
                )
            for _ in range(8):
                if await self.reconcile_entry(tid):
                    break
                await asyncio.sleep(1)
            if self.store.trade(tid)["status"] == "INTENT":
                self.alert(
                    "ENTRY:" + tid,
                    "reconcile",
                    {"trade_id": tid},
                    RuntimeError("成交状态尚未确认；禁止重复开仓"),
                )
            return self.store.trade(tid)["status"]

    async def reconcile_entry(self, tid):
        t = self.store.trade(tid)
        if not t or t["status"] != "INTENT":
            return True
        d = json.loads(t["details"])
        order = await self.exchange.order(t["symbol"], d["entry_link"])
        if not order:
            return False
        self.store.execute(
            "UPDATE orders SET receipt=?,exchange_id=?,status=? WHERE link_id=?",
            (encode(order), order["orderId"], order["orderStatus"], d["entry_link"]),
        )
        if order["orderStatus"] not in TERMINAL:
            return False
        filled = number(order.get("cumExecQty") or 0)
        if filled <= 0:
            self.store.update_trade(tid, status="UNFILLED", order_id=order["orderId"])
            self.store.signal_result(t["signal_id"], "UNFILLED")
            self.store.resolve("ENTRY:" + tid)
            return True
        entry = number(order["avgPrice"])
        if entry <= 0 or filled > number(t["qty"]):
            raise ValueError("Entry fill geometry mismatch")
        # Terminal order aggregates are exchange-confirmed fills and paid fees.
        # Do not leave a filled position unprotected while execution history lags.
        fee_detail = order.get("cumFeeDetail")
        executions = []
        if isinstance(fee_detail, dict) and set(fee_detail) == {"USDT"}:
            if (
                order.get("symbol") != t["symbol"]
                or order.get("orderLinkId") != d["entry_link"]
                or order.get("side") != ("Buy" if t["side"] == "LONG" else "Sell")
                or int(order.get("positionIdx", -1)) != t["position_idx"]
                or order.get("reduceOnly") is not False
                or abs(number(order["cumExecValue"]) - entry * filled) > number(d["tick"]) * filled
            ):
                raise ValueError("Entry aggregate identity/value mismatch")
            entry_fee = number(fee_detail["USDT"])
            created = int(order["createdTime"]) / 1000
            order_updated_at = int(order["updatedTime"]) / 1000
            if not d["intent_at"] - 5 <= created <= order_updated_at <= time.time() + 5:
                raise ValueError("Entry aggregate timestamp mismatch")
            opened = created  # Conservative history start until exact fills arrive.
            d.update(
                entry_fee_source="order_cumFeeDetail",
                entry_evidence_pending=True,
                entry_order_snapshot=order,
                opened_at_source="order_created_time",
            )
        else:
            executions = await self.exchange.executions(
                t["symbol"], order["orderId"], at=d["intent_at"]
            )
            executions = [
                e
                for e in executions
                if e.get("execType") == "Trade" and e.get("orderId") == order["orderId"]
            ]
            if sum((number(e["execQty"]) for e in executions), D(0)) != filled:
                self.store.event(
                    "ENTRY_EVIDENCE_PENDING",
                    {
                        "trade_id": tid,
                        "order_status": order["orderStatus"],
                        "filled_qty": str(filled),
                        "execution_count": len(executions),
                        "has_usdt_fee": isinstance(fee_detail, dict) and "USDT" in fee_detail,
                    },
                )
                return False
            entry_fee = sum((number(e["execFee"]) for e in executions), D(0))
            opened = min(int(e["execTime"]) for e in executions) / 1000
            d.update(entry_fee_source="executions", opened_at_source="executions")
        tp = tp_price(
            t["side"],
            entry,
            filled,
            number(d["tick"]),
            number(t["tp_target_net_pnl"]),
            entry_fee,
            number(d["maker"]),
        )
        sl = sl_price(
            t["side"],
            entry,
            filled,
            number(d["tick"]),
            entry_fee,
            number(d["taker"]),
            loss=number(d.get("sl_loss", "8")),  # Legacy intents retain their original budget.
        )
        d.update(entry_fee=str(entry_fee), entry_executions=executions)
        updated = {**t, "entry_price": str(entry), "qty": str(filled)}
        active = [p for p in await self.exchange.positions(t["symbol"]) if number(p["size"]) > 0]
        if active and not any(matches_position(updated, p) for p in active):
            raise ValueError(
                "Exchange position differs from confirmed own fill; manual reconciliation required"
            )
        self.store.update_trade(
            tid,
            status="OPEN" if active else "SETTLING",
            entry_price=str(entry),
            qty=str(filled),
            margin=str(filled * entry / number(t["leverage"])),
            tp_price=str(tp),
            sl_price=str(sl),
            order_id=order["orderId"],
            opened_at=opened,
            details=encode(d),
        )
        self.store.signal_result(t["signal_id"], "OPEN", t["side"])
        self.store.resolve("ENTRY:" + tid)
        if active:
            await self.protect(tid)
        return True

    async def protective_order(self, tid, kind, *, manual=False):
        t = self.store.trade(tid)
        if not t or not t["owned"] or t["status"] != "OPEN":
            return True
        active = [p for p in await self.exchange.positions(t["symbol"]) if number(p["size"]) > 0]
        if not active:
            return True
        if not any(matches_position(t, p) for p in active):
            raise ValueError("仓位身份变化，不能补挂原仓位保护单")
        remaining = number(next(p["size"] for p in active if matches_position(t, p)))
        rows = self.store.rows(
            "SELECT * FROM orders WHERE trade_id=? AND kind=? ORDER BY created_at DESC", (tid, kind)
        )
        total_attempts = sum(r["attempts"] for r in rows)
        row = rows[0] if rows else None
        current = await self.exchange.order(t["symbol"], row["link_id"]) if row else None
        if current and current["orderStatus"] in LIVE:
            self.verify_protection(t, kind, current, remaining)
            self.record_protection(tid, kind, current)
            return True
        if current and current["orderStatus"] == "Filled":
            self.record_protection(tid, kind, current)
            if not manual:
                return True
            if time.time() - int(current.get("updatedTime", 0)) / 1000 < 5:
                raise RuntimeError("成交与仓位同步中，请稍后点击重试")
        if row and current and current["orderStatus"] in TERMINAL:
            if not manual and total_attempts >= 2:
                raise RuntimeError(f"{kind}已取消或拒绝，需点击重试补挂")
            row = None
        if row and not current and row["status"] == "Rejected" and (manual or total_attempts < 2):
            row = None
        if row is None:
            generation = len(rows)
            link = f"lt-{kind.lower()}-" + identity(tid, kind, generation)
            payload = {
                "category": "linear",
                "symbol": t["symbol"],
                "side": "Sell" if t["side"] == "LONG" else "Buy",
                "qty": format(remaining, "f"),
                "positionIdx": t["position_idx"],
                "reduceOnly": True,
                "orderLinkId": link,
            }
            if kind == "TP":
                payload.update(orderType="Limit", timeInForce="GTC", price=format(number(t["tp_price"]), "f"))
            else:
                payload.update(
                    orderType="Market",
                    triggerPrice=format(number(t["sl_price"]), "f"),
                    triggerBy="LastPrice",
                    triggerDirection=2 if t["side"] == "LONG" else 1,
                    closeOnTrigger=True,
                )
            self.store.execute(
                "INSERT INTO orders(link_id,trade_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                (link, tid, kind, encode(payload), time.time()),
            )
            row = self.store.rows("SELECT * FROM orders WHERE link_id=?", (link,))[0]
        link = row["link_id"]
        budget = 1 if manual else max(0, 2 - total_attempts)
        for _ in range(budget):
            # Reconcile before every retry; persist attempts before the network call.
            existing = await self.exchange.order(t["symbol"], link)
            if existing and existing["orderStatus"] in LIVE:
                self.verify_protection(t, kind, existing, remaining)
                self.record_protection(tid, kind, existing)
                return True
            if existing and existing["orderStatus"] == "Filled":
                self.record_protection(tid, kind, existing)
                return True
            if existing and existing["orderStatus"] in TERMINAL:
                return await self.protective_order(tid, kind, manual=manual)

            # Change an unsent/definitively rejected intent only; never change an
            # uncertain request under the same id. Recheck position before exit.
            fresh_row = self.store.rows("SELECT * FROM orders WHERE link_id=?", (link,))[0]
            payload = json.loads(fresh_row["payload"])
            if fresh_row["attempts"] == 0 or fresh_row["status"] == "Rejected":
                last = await self.exchange.last_price(t["symbol"])
                target = number(t[kind.lower() + "_price"])
                favorable = (last - target) * (1 if t["side"] == "LONG" else -1)
                crossed = favorable <= 0 if kind == "SL" else favorable >= 0
                if crossed:
                    positions = await self.exchange.positions(t["symbol"])
                    matching = [p for p in positions if matches_position(t, p)]
                    if not matching:
                        if any(number(p["size"]) > 0 for p in positions):
                            raise ValueError("仓位身份变化，不能执行原目标退出")
                        return True
                    payload = {
                        k: v
                        for k, v in payload.items()
                        if k not in {"price", "triggerPrice", "triggerBy", "triggerDirection"}
                    }
                    payload.update(
                        orderType="Market",
                        timeInForce="IOC",
                        closeOnTrigger=True,
                        qty=format(number(matching[0]["size"]), "f"),
                    )
                    self.store.execute(
                        "UPDATE orders SET payload=? WHERE link_id=?", (encode(payload), link)
                    )
                    self.store.event(
                        "PROTECTION_TARGET_CROSSED",
                        {
                            "trade_id": tid,
                            "kind": kind,
                            "link_id": link,
                            "target": str(target),
                            "last_price": str(last),
                            "qty": payload["qty"],
                        },
                    )
            self.store.execute(
                "UPDATE orders SET attempts=attempts+1,status='SUBMITTING' WHERE link_id=?", (link,)
            )
            try:
                oid = await self.exchange.submit(payload)
                self.store.execute("UPDATE orders SET exchange_id=? WHERE link_id=?", (oid, link))
            except Exception as error:
                diagnostic = (
                    str(error)[:600]
                    if isinstance(error, (BybitAPIError, ValueError, RuntimeError))
                    else type(error).__name__
                )
                log.error("%s submit failure trade=%s link=%s: %s", kind, tid, link, diagnostic)
                if (
                    isinstance(error, BybitAPIError)
                    and error.definitive_rejection
                    and error.code > 0
                    and error.code not in {10000, 10014, 10016, 10019, 110072}
                ):
                    self.store.execute(
                        "UPDATE orders SET status='Rejected',receipt=? WHERE link_id=?",
                        (encode({"retCode": error.code, "retMsg": error.message}), link),
                    )
                self.store.event(
                    "PROTECTION_SUBMIT_FAILED",
                    {"trade_id": tid, "kind": kind, "link_id": link, "error": diagnostic},
                )
            for _ in range(3):
                await asyncio.sleep(0.4)
                actual = await self.exchange.order(t["symbol"], link)
                if actual and actual["orderStatus"] in LIVE:
                    self.verify_protection(t, kind, actual, remaining)
                    self.record_protection(tid, kind, actual)
                    return True
                if actual and actual["orderStatus"] == "Filled":
                    self.record_protection(tid, kind, actual)
                    return True
                if not any(
                    number(p["size"]) > 0 for p in await self.exchange.positions(t["symbol"])
                ):
                    return True
        failed = self.store.rows("SELECT receipt FROM orders WHERE link_id=?", (link,))
        receipt = json.loads(failed[0]["receipt"] or "{}") if failed else {}
        if receipt.get("retCode") in (110092, 110093):
            raise RuntimeError(
                f"{kind}原触发价已被行情越过，交易所拒单({receipt['retCode']})；"
                "原目标市价退出仍未确认；自动预算已用完，请点击重试"
            )
        raise RuntimeError(f"{kind}未能确认，自动重试预算已用完；请点击重试核对原目标退出")

    def verify_protection(self, t, kind, order, remaining=None):
        if (
            order.get("reduceOnly") is not True
            or order.get("symbol") != t["symbol"]
            or int(order.get("positionIdx", -1)) != t["position_idx"]
            or order.get("side") != ("Sell" if t["side"] == "LONG" else "Buy")
        ):
            raise ValueError("Protection identity/reduceOnly mismatch")
        expected = number(t["qty"]) if remaining is None else remaining
        coverage = (
            number(order.get("leavesQty") or order["qty"]) if kind == "TP" else number(order["qty"])
        )
        if not expected <= coverage <= number(t["qty"]):
            raise ValueError("Protection quantity does not cover the remaining position")
        submitted = self.store.rows(
            "SELECT payload FROM orders WHERE link_id=?", (order.get("orderLinkId"),)
        )
        payload = json.loads(submitted[0]["payload"]) if submitted else {}
        if payload.get("orderType") == "Market" and "triggerPrice" not in payload:
            if order.get("orderType") != "Market" or not order.get("closeOnTrigger"):
                raise ValueError("Crossed-target market exit geometry mismatch")
            return
        if kind == "TP":
            valid = (
                order.get("orderType") == "Limit"
                and order.get("timeInForce") == "GTC"
                and number(order["price"]) == number(t["tp_price"])
            )
        else:
            valid = (
                order.get("orderType") == "Market"
                and order.get("closeOnTrigger") is True
                and number(order["triggerPrice"]) == number(t["sl_price"])
                and int(order.get("triggerDirection", 0)) == (2 if t["side"] == "LONG" else 1)
                and order.get("triggerBy") == "LastPrice"
            )
        if not valid:
            raise ValueError("Protection order geometry mismatch")

    def record_protection(self, tid, kind, order):
        self.store.update_trade(tid, **{kind.lower() + "_order_id": order["orderId"]})
        self.store.execute(
            "UPDATE orders SET exchange_id=?,status=?,receipt=? WHERE link_id=?",
            (order["orderId"], order["orderStatus"], encode(order), order["orderLinkId"]),
        )
        self.store.resolve(kind + ":" + tid)

    async def protect(self, tid, manual_kind=None):
        success = True
        # SL first minimizes the initial unprotected interval; each leg has its own budget.
        for kind in [manual_kind] if manual_kind else ["SL", "TP"]:
            try:
                await self.protective_order(tid, kind, manual=manual_kind is not None)
            except Exception as error:
                success = False
                self.alert(kind + ":" + tid, "protection", {"trade_id": tid, "kind": kind}, error)
        t = self.store.trade(tid)
        if t and t["opened_at"]:
            protection_status = (
                "止盈止损已挂好"
                if t["tp_order_id"] and t["sl_order_id"]
                else "⚠️ 保护单未齐，请查看异常通知"
            )
            self.store.queue(
                "opened:" + tid,
                f"✅ 开仓 · {t['symbol']} · {'做多' if t['side'] == 'LONG' else '做空'}\n"
                f"保证金 {D(t['margin']):.2f}U · {t['leverage']}倍\n"
                f"成交 {t['entry_price']}\n"
                f"止盈 {t['tp_price']} · 止损 {t['sl_price']}\n"
                f"{protection_status}",
            )
        return success
