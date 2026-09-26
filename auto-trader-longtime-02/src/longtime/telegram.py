"""Durable important-event outbox and owner-only inline retry controls."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import httpx

from longtime import emergency, settings_controls
from longtime.notices import incident_text
from longtime.polling import STATE_KEY, PollingGuard
from longtime.trading_settings import EntryDefaults

log = logging.getLogger(__name__)
RATE_LIMIT_KEY = "telegram_rate_limit_until"


def main_keyboard(paused):
    return {
        "keyboard": [
            ["📊 当前持仓", "🧭 运行状态"],
            ["📥 最近开仓结果", "▶️ 恢复开仓" if paused else "⏸️ 暂停开仓"],
            ["⚠️ 最近异常", "⚙️ 开仓设置"],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
        "input_field_placeholder": "点击查看状态或控制新开仓",
    }


def local_time(value):
    return datetime.fromtimestamp(float(value), ZoneInfo("Asia/Shanghai")).strftime(
        "%m-%d %H:%M:%S"
    )


def direction(value):
    return {
        "LONG": "做多",
        "SHORT": "做空",
        "SKIP": "观望",
        "Buy": "做多",
        "Sell": "做空",
    }.get(value, value)


class TelegramAPIError(RuntimeError):
    def __init__(self, message, *, transient=False):
        super().__init__(message)
        self.transient = transient


class Telegram:
    def __init__(self, settings, store, client=None):
        self.settings, self.store = settings, store
        self.client = client or httpx.AsyncClient(timeout=40)
        self.token = settings.telegram_token.get_secret_value()
        self.poll_client = client or self.new_poll_client()
        self.owns_poll_client = client is None
        self.polling = PollingGuard(store, self.token)
        self.poll_lock = asyncio.Lock()
        self.tasks = set()
        self.delivery_lock = asyncio.Lock()
        self._offset = 0
        self._poll_healthy_since: float | None = None
        self._poll_retry_at = 0.0
        self._poll_failures = 0

    async def call(self, method, payload=None):
        # Shared durable cooldown: a poll rejection must also stop notification
        # requests. Sleep in cancellable chunks without consuming retry budgets.
        while self.store:
            try:
                delay = self.store.state(RATE_LIMIT_KEY, 0) - time.time()
            except sqlite3.Error:
                # The independent emergency channel must work without SQLite.
                break
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, 30))
        if method != "getUpdates":
            return await self._call(method, payload)
        try:
            async with self.polling.request():
                try:
                    return await self._call(method, payload)
                except (Exception, asyncio.CancelledError):
                    if self.owns_poll_client:
                        await self.poll_client.aclose()
                        self.poll_client = self.new_poll_client()
                    raise
        except TimeoutError:
            raise TelegramAPIError(
                "Telegram getUpdates failed (TimeoutError)", transient=True
            ) from None

    @staticmethod
    def new_poll_client():
        return httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))

    async def _call(self, method, payload=None):
        payload = dict(payload or {})
        markup = payload.get("reply_markup")
        if isinstance(markup, dict) and "remove_keyboard" in markup:
            raise ValueError("Removing the main reply keyboard is forbidden")
        if method.startswith("editMessage") and isinstance(markup, dict) and "keyboard" in markup:
            raise ValueError("Message edits only support inline keyboards")
        if method == "sendMessage" and (
            markup is None or isinstance(markup, dict) and "keyboard" in markup
        ):
            # Canonical navigation, including legacy queued keyboards and direct sends.
            # Inline keyboards remain message-local; never clear the chat reply keyboard.
            payload["reply_markup"] = main_keyboard(
                self.store.state("entries_paused", False) if self.store else False
            )
        try:
            client = self.poll_client if method == "getUpdates" else self.client
            r = await client.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=payload or {}
            )
            if r.status_code >= 500:
                raise TelegramAPIError(
                    f"Telegram {method} rejected (HTTP {r.status_code})", transient=True
                )
            try:
                d = r.json()
            except ValueError:
                if r.status_code != 429:
                    raise
                d = {}
            if r.status_code == 429 or d.get("error_code") == 429:
                parameters = d.get("parameters")
                retry_after = (
                    parameters.get("retry_after") if isinstance(parameters, dict) else None
                )
                # Missing/malformed server guidance never becomes a tight retry loop.
                seconds = retry_after if type(retry_after) is int and retry_after > 0 else 60
                if self.store:
                    self.store.set(
                        RATE_LIMIT_KEY,
                        max(self.store.state(RATE_LIMIT_KEY, 0), time.time() + seconds),
                    )
                    self.store.event(
                        "TELEGRAM_RATE_LIMIT",
                        {
                            "method": method,
                            "retry_after": seconds,
                            "server_retry_after": seconds == retry_after,
                        },
                    )
                raise TelegramAPIError(
                    f"Telegram {method} rate limited (429, retry_after={seconds}s)",
                    transient=True,
                )
            if r.status_code >= 400 or d.get("ok") is not True:
                raise TelegramAPIError(
                    f"Telegram {method} rejected (HTTP {r.status_code}, code {d.get('error_code')})"
                )
            return d["result"]
        except (httpx.HTTPError, ValueError) as error:
            # Do not leak the token-bearing URL via exception text or exception chaining.
            raise TelegramAPIError(
                f"Telegram {method} failed ({type(error).__name__})",
                transient=isinstance(error, httpx.TransportError),
            ) from None

    async def preflight(self):
        me = await self.call("getMe")
        hook = await self.call("getWebhookInfo")
        if hook.get("url"):
            raise ValueError("Bot has an existing webhook; configure an independent bot")
        return {"id": me["id"], "username": me.get("username")}

    async def deliver_emergency(self):
        data = emergency.load(self.settings.runtime_dir)
        if not data or data["sent"] or data["attempts"] >= 2:
            return
        data["attempts"] += 1
        emergency.save(self.settings.runtime_dir, data)
        try:
            await self.call(
                "sendMessage",
                {
                    "chat_id": self.settings.telegram_chat_id,
                    "text": "⚠️ 数据库异常\n\n"
                    + data["scope"]
                    + "\n交易状态将重新核对，请点击重试检查数据库恢复。",
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "🔄 重试", "callback_data": "retry:database"}]
                        ]
                    },
                },
            )
            data["sent"] = True
            emergency.save(self.settings.runtime_dir, data)
        except Exception:
            log.error("Emergency Telegram delivery failed; local error retained")

    async def deliver(self):
        async with self.delivery_lock:
            await self._deliver()

    async def _deliver(self):
        try:
            if self.store.state(RATE_LIMIT_KEY, 0) > time.time():
                return
        except sqlite3.Error:
            pass  # Deliver the independent database emergency notice first.
        await self.deliver_emergency()
        for row in self.store.rows(
            "SELECT * FROM outbox WHERE status='PENDING' AND next_attempt<=? ORDER BY rowid LIMIT 10",
            (time.time(),),
        ):
            if self.store.state(RATE_LIMIT_KEY, 0) > time.time():
                break
            if row["attempts"] >= 2:
                # A process may stop after submitting but before persisting the reply.
                # The already-consumed automatic budget must survive that restart.
                self.store.execute(
                    "UPDATE outbox SET status='FAILED' WHERE event_key=?", (row["event_key"],)
                )
                if not row["event_key"].startswith("alert:"):
                    self.store.incident(
                        "TELEGRAM_DELIVERY",
                        "delivery",
                        {},
                        "通知自动投递预算已耗尽，发送结果未确认；可人工重试",
                    )
                continue
            attempt = row["attempts"] + 1
            self.store.execute(
                "UPDATE outbox SET attempts=? WHERE event_key=?",
                (
                    attempt,
                    row["event_key"],
                ),
            )
            try:
                payload = {"chat_id": self.settings.telegram_chat_id, "text": row["text"]}
                if row["markup"]:
                    payload["reply_markup"] = json.loads(row["markup"])
                else:
                    payload["reply_markup"] = main_keyboard(
                        self.store.state("entries_paused", False)
                    )
                sent = await self.call("sendMessage", payload)
                self.store.execute(
                    "UPDATE outbox SET status='SENT',message_id=? WHERE event_key=?",
                    (sent["message_id"], row["event_key"]),
                )
                if not self.store.rows(
                    "SELECT 1 FROM outbox WHERE status IN ('FAILED','PENDING') AND attempts>0 LIMIT 1"
                ):
                    self.store.resolve("TELEGRAM_DELIVERY")
            except Exception as error:
                self.store.execute(
                    "UPDATE outbox SET attempts=?,next_attempt=?,status=? WHERE event_key=?",
                    (
                        attempt,
                        max(time.time() + 30, self.store.state(RATE_LIMIT_KEY, 0)),
                        "FAILED" if attempt >= 2 else "PENDING",
                        row["event_key"],
                    ),
                )
                log.error(
                    "Telegram delivery failed event=%s: %s",
                    row["event_key"],
                    str(error) if isinstance(error, TelegramAPIError) else type(error).__name__,
                )
                # Delivery failure cannot reliably alert through the same failed transport.
                # Persist a single incident for recovery; avoid an alert-of-alert loop.
                if not row["event_key"].startswith("alert:"):
                    self.store.incident(
                        "TELEGRAM_DELIVERY",
                        "delivery",
                        {},
                        "Telegram发送失败；日志已记录，可点击重试重新投递失败消息",
                    )

    def authorized(self, query):
        message = query.get("message", {})
        return (
            query.get("from", {}).get("id") == self.settings.telegram_user_id
            and message.get("chat", {}).get("id") == self.settings.telegram_chat_id
        )

    async def acknowledge(self, payload):
        # Acknowledgement is UI feedback, not the durable business action. Failure
        # must never replay an update or strand the subsequent queue behind it.
        try:
            await self.call("answerCallbackQuery", payload)
        except RuntimeError as error:
            log.warning(
                "Callback acknowledgement failed callback=%s: %s",
                payload["callback_query_id"],
                error,
            )

    async def handle(self, update, retry):
        q = update.get("callback_query")
        if q:
            if not self.authorized(q):
                await self.acknowledge({"callback_query_id": q["id"], "text": "无权限"})
                return
            data = q.get("data", "")
            if data.startswith("settings:"):
                settings_controls.process(
                    self.store,
                    "callback:" + q["id"],
                    action=data.removeprefix("settings:"),
                    actor=q["from"]["id"],
                    message_id=q.get("message", {}).get("message_id"),
                )
                await self.acknowledge({"callback_query_id": q["id"], "text": "已处理"})
                return
            if data == "retry:database":
                try:
                    with self.store.connect() as db:
                        claimed = db.execute(
                            "INSERT OR IGNORE INTO callbacks VALUES (?,?)", (q["id"], time.time())
                        ).rowcount
                        if claimed:
                            db.execute(
                                "INSERT INTO state VALUES ('database_recovery_probe',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (str(time.time()),),
                            )
                    if claimed:
                        emergency.clear(self.settings.runtime_dir)
                    message = (
                        "数据库读写已恢复，后台将继续核对真实状态" if claimed else "该次检查已处理"
                    )

                except sqlite3.Error:
                    message = "数据库仍不可用；未执行交易，请修复后再次重试"
                await self.acknowledge(
                    {"callback_query_id": q["id"], "text": message, "show_alert": True}
                )
                return
            iid = data.removeprefix("retry:")
            if not data.startswith("retry:") or not self.store.claim_callback(q["id"], iid):
                await self.acknowledge({"callback_query_id": q["id"], "text": "已处理或正在重试"})
                return
            await self.acknowledge(
                {"callback_query_id": q["id"], "text": "正在核对最新状态并重试一次"}
            )
            task = asyncio.create_task(self.run_retry(iid, q["id"], retry))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        m = update.get("message", {})
        if not (
            m.get("from", {}).get("id") == self.settings.telegram_user_id
            and m.get("chat", {}).get("id") == self.settings.telegram_chat_id
        ):
            return
        raw = m.get("text", "").strip()
        command = {
            "📊 当前持仓": "/positions",
            "🧭 运行状态": "/status",
            "📥 最近开仓结果": "/entry_results",
            "🧾 最近交易": "/history",
            "🧠 最近分析": "/analysis",
            "▶️ 恢复开仓": "/resume",
            "⏸️ 暂停开仓": "/pause",
            "⚠️ 异常处理": "/alerts",
            "⚠️ 最近异常": "/alerts",
            "⚙️ 开仓设置": "/settings",
            "⚙️ 策略说明": "/strategy",
            "🔄 重发失败通知": "/retry_delivery",
        }.get(raw, raw.split(" ")[0].split("@")[0])
        if command == "/settings":
            settings_controls.process(self.store, str(update["update_id"]), action="view")
            return
        if self.store.state(settings_controls.PENDING) and (
            command == "/cancel" or not command.startswith("/")
        ):
            settings_controls.process(
                self.store,
                str(update["update_id"]),
                text=raw,
                actor=m["from"]["id"],
                message_id=m.get("message_id"),
            )
            return
        if self.store.state(settings_controls.PENDING):
            self.store.set(settings_controls.PENDING, None)
        markup = None
        if command == "/pause":
            self.store.set("entries_paused", True)
            text = "已暂停新开仓。已有仓位继续执行止盈、对冲和双仓判向。"
        elif command == "/resume":
            self.store.set("entries_paused", False)
            text = (
                "已恢复新开仓，等待分析系统发布新的有效信号。"
                if self.settings.trading_enabled
                else "已恢复信号接收；当前为只读验证模式，不提交订单。"
            )
        elif command in ("/start", "/status"):
            text = self.status_text()
        elif command == "/positions":
            text = self.positions_text()
        elif command == "/analysis":
            text = self.analysis_text()
        elif command == "/entry_results":
            text = self.entry_results_text()
        elif command == "/alerts":
            rows = self.store.rows(
                "SELECT * FROM incidents WHERE status IN ('OPEN','RUNNING') ORDER BY created_at DESC LIMIT 5"
            )
            text = "✅ 最近无异常。"
            if rows:
                messages, buttons = [], []
                for row in rows:
                    data = json.loads(row["payload"])
                    trade = self.store.trade(data["trade_id"]) if data.get("trade_id") else None
                    symbol = trade["symbol"] if trade else data.get("symbol")
                    messages.append(
                        "发生时间："
                        + local_time(row["created_at"])
                        + "\n"
                        + incident_text(
                            row["scope"], row["kind"], row["error"], row["incident_id"], symbol
                        )
                    )
                    buttons.append(
                        [
                            {
                                "text": "🔄 检查 " + (symbol or row["incident_id"][:8]),
                                "callback_data": "retry:" + row["incident_id"],
                            }
                        ]
                    )
                text = "待处理异常（最近5项）\n\n" + "\n\n".join(messages)
                markup = {"inline_keyboard": buttons}
        elif command in ("/strategy", "/help"):
            text = (
                "⚙️ 交易执行系统 · Bybit Demo\n"
                "等待single-analysis信号，发布后10分钟有效；重复或过期信号不执行。\n"
                "保留资金、精度和杠杆检查；暂不使用止盈可达性过滤。\n"
                "无平仓止损；对冲全成后等待公共信号，有效判向后平逆向仓并重新挂止盈与对冲。\n"
                "分析与频率设置请使用分析Bot。\n\n" + EntryDefaults.load(self.store).description()
            )
        elif command == "/history":
            rows = self.store.rows(
                "SELECT symbol,side,net_pnl,close_reason FROM trades WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 10"
            )
            text = "最近平仓\n" + (
                "\n".join(
                    f"{r['symbol']} · {direction(r['side'])} · {D(r['net_pnl']):+.4f}U · "
                    + {
                        "TP": "止盈",
                        "SL": "旧策略止损",
                        "HEDGE_CLOSE": "判向平仓",
                        "HEDGE_CLEANUP": "对冲余仓平仓",
                        "MIXED": "分笔平仓",
                        "OTHER": "其他平仓",
                        "LIQUIDATION": "强平",
                    }.get(r["close_reason"], "已平仓")
                    for r in rows
                )
                or "暂无记录"
            )
        elif command == "/retry_delivery":
            self.store.execute(
                "UPDATE outbox SET status='PENDING',attempts=0,next_attempt=0 WHERE status='FAILED'"
            )
            text = "已将失败通知加入重试队列。"
        else:
            return
        self.store.queue("command:" + str(update["update_id"]), text, markup)

    def status_text(self):
        paused = self.store.state("entries_paused", False)
        count = self.store.rows("SELECT count(*) n FROM trades WHERE status='OPEN' AND owned=1")[0][
            "n"
        ]
        settling = self.store.rows("SELECT count(*) n FROM trades WHERE status='SETTLING'")[0]["n"]
        incidents = self.store.rows(
            "SELECT count(*) n FROM incidents WHERE status IN ('OPEN','RUNNING')"
        )[0]["n"]
        heartbeat = self.store.state("monitor_heartbeat", 0)
        fresh = heartbeat and time.time() - heartbeat < 90
        mode = "Demo自动交易" if self.settings.trading_enabled else "只读验证"
        state = "⏸️ 已暂停新开仓" if paused else "▶️ 新开仓已启用"
        signal_time = self.store.state("signal_heartbeat", 0)
        signal_fresh = signal_time and time.time() - signal_time < 15
        return (
            f"🧭 {mode}\n{state}\n"
            f"持仓 {count}笔 · 待结算 {settling}笔 · 待处理异常 {incidents}项\n"
            f"信号接收：{'正常' if signal_fresh else '尚未连接或延迟'}\n"
            "分析频率：由分析Bot管理\n"
            f"仓位检查：{'正常' if fresh else '更新延迟，请查看异常处理'}\n"
            "信号来源：single-analysis"
        )

    def positions_text(self):
        positions = self.store.state("exchange_positions", [])
        heartbeat = self.store.state("monitor_heartbeat", 0)
        lines = [
            "📊 交易所持仓",
            "更新：" + (local_time(heartbeat) if heartbeat else "尚未完成核对"),
        ]
        if not heartbeat or time.time() - heartbeat > 90:
            lines.append("⚠️ 快照更新延迟，请查看异常处理。")
        for p in positions[:20]:
            rows = self.store.rows(
                "SELECT tp_price,sl_price FROM trades WHERE symbol=? AND position_idx=? AND status='OPEN'",
                (p["symbol"], int(p.get("positionIdx", 0))),
            )
            text = f"{p['symbol']} · {direction(p['side'])}\n均价 {p['avgPrice']} · 浮盈亏 {D(p.get('unrealisedPnl') or '0'):+.4f}U"
            groups = [
                json.loads(r["document"])
                for r in self.store.rows("SELECT document FROM hedge_groups")
            ]
            locked = any(g["symbol"] == p["symbol"] and g["phase"] == "LOCKED" for g in groups)
            if locked:
                text += "\n双向持仓等待判向 · 无止盈止损"
            elif rows:
                text += f"\n原止盈 {rows[0]['tp_price']} · 对冲价 {rows[0]['sl_price']}"
            lines.append(text)
        if len(positions) > 20:
            lines.append(f"共{len(positions)}笔，仅展示前20笔。")
        return "\n\n".join(lines) if positions else "\n".join(lines + ["暂无持仓"])

    def analysis_text(self):
        return "选币、方向分析、分析异常和频率设置已迁移至独立分析Bot。"

    def entry_results_text(self):
        latest = self.store.rows(
            "SELECT cycle_id,MAX(created_at) latest FROM signals "
            "GROUP BY cycle_id ORDER BY latest DESC LIMIT 1"
        )
        if not latest:
            return "📥 最近一轮分析开仓结果\n\n暂无收到的分析方向信号。"
        rows = self.store.rows(
            "SELECT * FROM signals WHERE cycle_id=? ORDER BY created_at,signal_id",
            (latest[0]["cycle_id"],),
        )
        evidence = json.loads(rows[0]["evidence"])
        analyzed_at = evidence.get("analysis_started_at", evidence.get("published_at"))
        lines = [
            "📥 最近一轮分析开仓结果",
            "🕒 分析时间："
            + (local_time(analyzed_at) + "（北京时间）" if analyzed_at else "时间未记录"),
            f"📡 本系统共收到 {len(rows)} 条方向信号",
        ]
        labels = {
            "SELECTED": "⏳ 已接收，正在处理",
            "OPEN": "✅ 开仓成功",
            "HEDGE_DIRECTION_APPLIED": "✅ 双仓判向已执行",
            "SKIP_MODEL": "⏭️ 分析结论为观望，未开仓",
            "SKIP_EXISTING_POSITION": "⏭️ 已有该币仓位，未重复开仓",
            "SKIP_EXISTING_ORDERS": "⏭️ 已有该币在途订单，未开仓",
            "SKIP_TP_UNREACHABLE": "⏭️ 止盈可达性未通过，未开仓",
            "SKIP_PAUSED": "⏸️ 当时已暂停新开仓",
            "SKIP_SIGNAL_EXPIRED": "⏭️ 信号处理前已过期",
            "SKIP_CYCLE_ENDED": "⏭️ 下单前信号已过期",
            "SKIP_SETTINGS_CHANGED": "⏭️ 处理期间开仓设置已变化",
            "SKIP_NOT_NORMAL_CANDIDATE": "⏭️ 仅供双仓复核，不是普通开仓候选",
            "SKIP_HEDGE_GENERATION": "⏭️ 双仓轮次已变化，未执行旧信号",
            "STOP_INSUFFICIENT_MARGIN": "⛔ 可用资金不足，未开仓",
            "DRY_RUN_ELIGIBLE": "🔎 只读验证通过，未提交订单",
            "UNFILLED": "❌ 已提交但未成交",
            "INTERRUPTED": "⚠️ 处理被中断，旧信号不会重放",
            "ERROR": "⚠️ 处理异常，未确认开仓成功",
        }
        for index, row in enumerate(rows, 1):
            item = json.loads(row["evidence"])
            analyzed_side = direction(item.get("decision", row["side"] or "SKIP"))
            kind = " · 双仓复核" if item.get("hedge") else ""
            result = labels.get(row["status"], "⏳ 状态正在核对")
            trade = self.store.rows(
                "SELECT entry_price,qty FROM trades WHERE signal_id=? "
                "ORDER BY COALESCE(opened_at,0) DESC LIMIT 1",
                (row["signal_id"],),
            )
            if row["status"] == "OPEN" and trade:
                details = trade[0]
                filled = []
                if details["entry_price"]:
                    filled.append("成交价 " + details["entry_price"])
                if details["qty"]:
                    filled.append("数量 " + details["qty"])
                if filled:
                    result += " · " + " · ".join(filled)
            lines += ["", f"{index}. {row['symbol']} · {analyzed_side}{kind}", result]
        return "\n".join(lines)

    async def run_retry(self, iid, callback_id, retry):
        rows = self.store.rows("SELECT * FROM incidents WHERE incident_id=?", (iid,))
        if not rows:
            return
        row = rows[0]
        ok = False
        try:
            ok = await retry(row, callback_id)
        except Exception as error:
            log.error("Manual retry failed: %s", type(error).__name__)
        finally:
            self.store.execute(
                "UPDATE incidents SET status=? WHERE incident_id=?",
                ("RESOLVED" if ok else "OPEN", iid),
            )
            self.store.queue(
                "retry-result:" + callback_id,
                "✅ 重试完成，已核对最新状态。"
                if ok
                else "⚠️ 重试仍未成功；未增加后台重试。可再次点击原消息的重试按钮。",
                None
                if ok
                else {"inline_keyboard": [[{"text": "🔄 重试", "callback_data": "retry:" + iid}]]},
            )

    async def poll(self, retry=None):
        async with self.poll_lock:
            await self._poll(retry)

    def poll_error(self, error, alert):
        now = time.time()
        failure = self.store.state("TELEGRAM_POLL:failure") or {"since": now, "count": 0}
        failure["count"] += 1
        self.store.set("TELEGRAM_POLL:failure", failure)
        safe = str(error)[:600] if isinstance(error, TelegramAPIError) else type(error).__name__
        self.store.event(
            "TELEGRAM_POLL_FAILURE",
            {
                "error": safe,
                "request": self.store.state(STATE_KEY),
                "count": failure["count"],
                "since": failure["since"],
            },
        )
        # Only known transport/5xx failures get an outage observation window.
        # Auth, conflicts, malformed data and command failures remain immediate.
        if (
            not isinstance(error, TelegramAPIError)
            or not error.transient
            or failure["count"] >= 3
            and now - failure["since"] >= 120
        ):
            alert("TELEGRAM_POLL", "monitor", {}, error)
        else:
            log.warning("TELEGRAM_POLL reconnecting: %s", safe)

    async def _poll(self, retry):
        # Back off only polling; never replay sends or consume another delivery attempt.
        delay = self._poll_retry_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self._poll_batch(retry)
        except Exception:
            self._poll_healthy_since = None
            self._poll_failures += 1
            self._poll_retry_at = time.monotonic() + min(30, 2 ** min(self._poll_failures, 5))
            raise
        self._poll_failures = 0
        self._poll_retry_at = 0.0
        self.store.set("TELEGRAM_POLL:failure", None)
        self.store.set("TELEGRAM_POLL:last_success", time.time())
        now = time.monotonic()
        if self._poll_healthy_since is None:
            self._poll_healthy_since = now
        # One successful batch between timeouts does not prove stable recovery.
        # Restart starts a new observation window; downtime never counts as health.
        if now - self._poll_healthy_since >= 300:
            self.store.resolve("TELEGRAM_POLL")

    async def _poll_batch(self, retry):
        try:
            offset = self.store.state("telegram_offset", self._offset)
        except sqlite3.Error:
            offset = self._offset
        updates = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        for update in updates:
            await self.handle(update, retry)
            self._offset = update["update_id"] + 1
            try:
                self.store.set("telegram_offset", self._offset)
            except sqlite3.Error:
                pass

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        if self.owns_poll_client:
            await self.poll_client.aclose()
        self.polling.close()
