"""Analysis-only owner bot with durable command processing and outbox."""

import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from analysis_core.notifications import cycle_keyboard, cycle_notice, signal_detail
from analysis_core.polling import STATE_KEY, PollingGuard
from analysis_core.scheduling import next_slot
from analysis_core.store import encode, identity


class BotAPIError(RuntimeError):
    def __init__(self, message, *, conflict=False):
        super().__init__(message)
        self.conflict = conflict


class AnalysisBot:
    def __init__(self, settings, store, client=None):
        self.settings, self.store = settings, store
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10))
        self.poll_client = client or self.new_poll_client()
        self.owns_poll_client = client is None
        self.polling = PollingGuard(store, settings.telegram_token.get_secret_value())
        self.poll_lock = asyncio.Lock()

    @staticmethod
    def new_poll_client():
        # Separate transport: reconnecting a failed long poll cannot disrupt delivery.
        return httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))

    async def call(self, method, payload=None):
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
            raise BotAPIError("分析Bot getUpdates 总请求超时：TimeoutError") from None

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
            # Normalize direct and persisted navigation; leave inline controls local.
            payload["reply_markup"] = self.keyboard()
        try:
            client = self.poll_client if method == "getUpdates" else self.client
            response = await client.post(
                "https://api.telegram.org/bot"
                + self.settings.telegram_token.get_secret_value()
                + "/"
                + method,
                json=payload or {},
            )
            doc = response.json()
            if response.status_code >= 400 or not doc.get("ok"):
                description = str(doc.get("description", "")).lower()
                reason = ""
                conflict = response.status_code == 409
                if conflict:
                    reason = (
                        "；webhook与轮询冲突" if "webhook" in description
                        else "；存在其他getUpdates请求" if "getupdates" in description
                        else "；Telegram轮询冲突，具体来源未确认"
                    )
                raise BotAPIError(
                    f"分析Bot {method} 请求失败，HTTP {response.status_code}{reason}",
                    conflict=conflict,
                )
            return doc["result"]
        except (httpx.HTTPError, ValueError) as error:
            raise BotAPIError(f"分析Bot {method} 连接失败：{type(error).__name__}") from None

    async def preflight(self):
        if not (
            self.settings.telegram_token.get_secret_value()
            and self.settings.telegram_chat_id
            and self.settings.telegram_user_id
        ):
            raise ValueError("请在single-analysis/.env填写独立分析Bot凭据")
        self.polling.startup()
        me = await self.call("getMe")
        hook = await self.call("getWebhookInfo")
        if hook.get("url"):
            raise ValueError("分析Bot存在webhook，不能启动轮询")
        return {"id": me["id"], "username": me.get("username")}

    def handle(self, update):
        callback = update.get("callback_query", {})
        message = callback.get("message", {}) if callback else update.get("message", {})
        if (
            message.get("chat", {}).get("id") != self.settings.telegram_chat_id
            or (callback or message).get("from", {}).get("id") != self.settings.telegram_user_id
        ):
            return
        text = message.get("text", "").strip()
        if callback:
            data = callback.get("data", "")
            if data.startswith("detail:"):
                text = data
            elif data.startswith("confirm:"):
                text = "/confirm " + data.removeprefix("confirm:")
            elif data.startswith("cancel:"):
                text = "/cancel " + data.removeprefix("cancel:")
            elif data in ("status", "recent", "errors", "interval", "pause", "resume", "retry_delivery"):
                text = "/" + data
            else:
                return
        if not callback and text.startswith("📊 "):
            text = "/recent"
        text = {
            "🧭 分析状态": "/status",
            "🧠 最近分析": "/recent",
            "⏱ 分析频率": "/interval",
            "⏸ 暂停分析": "/pause",
            "▶️ 恢复分析": "/resume",
            "⚠️ 分析异常": "/errors",
        }.get(text, text)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "INSERT OR IGNORE INTO callbacks VALUES (?,?)",
                ("bot:" + str(update["update_id"]), time.time()),
            ).rowcount:
                return

            def get(key, default=None):
                r = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                return json.loads(r[0]) if r else default

            def put(key, value):
                db.execute(
                    "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, encode(value)),
                )

            markup = None
            pending = get("interval_input")
            if pending and not text.startswith(("/", "detail:")):
                if time.time() >= pending["expires"]:
                    put("interval_input", None)
                    text = "/interval"
                else:
                    text = "/interval " + text
            if text in ("/status", "/start", "/recent", "/errors", "/pause", "/resume"):
                put("interval_input", None)
                put("interval_preview", None)
            reply = "请使用底部菜单选择操作。"
            if text in ("/status", "/start"):
                latest = db.execute("SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1").fetchone()
                due = get("next_analysis_at")
                paused = get("analysis_paused", False)

                def stamp(value):
                    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ) + "（Asia/Shanghai）"

                next_run = stamp(due) if due is not None else "待调度"
                if paused:
                    next_run = "已暂停，恢复后重新安排"
                elif due is not None and due <= time.time():
                    next_run = "等待当前轮完成后重新安排" if latest and latest["status"] == "RUNNING" else "待启动"
                selected = None
                if latest:
                    result = db.execute(
                        "SELECT payload FROM events WHERE kind='SCREENING_RESULT' "
                        "AND json_extract(payload, '$.cycle_id')=? ORDER BY created_at DESC LIMIT 1",
                        (latest["cycle_id"],),
                    ).fetchone()
                    if result:
                        selected = len(json.loads(result[0])["selected"])
                reply = "\n".join([
                    "🧭 系统状态",
                    f"服务状态: {'已暂停' if paused else '运行中'}",
                    f"分析频率: {get('analysis_interval', 20)} 分钟",
                    f"下次运行: {next_run}",
                    f"最后分析: {latest['cycle_id'] if latest else '暂无记录'}",
                    f"完成时间: {stamp(latest['completed_at']) if latest and latest['completed_at'] is not None else '尚未完成' if latest else '暂无记录'}",
                    f"本轮候选数: {selected if selected is not None else '暂无筛选结果'}",
                    f"分析状态: {latest['status'] if latest else '暂无记录'}",
                    "当前模式: Top10 筛选至 1–3 个小长线方向，首选明确 LONG/SHORT；双仓额外分析不占名额。",
                ])
            elif text == "/recent":
                rows = db.execute("SELECT cycle_id FROM cycles ORDER BY started_at DESC LIMIT 1").fetchall()
                reply = cycle_notice(self.store, rows[0][0]) if rows else "暂无分析记录，下一轮完成后会在这里显示。"
                if rows:
                    markup = cycle_keyboard(self.store, rows[0][0])
            elif text.startswith("detail:"):
                reply, markup = signal_detail(self.store, text.removeprefix("detail:"))
            elif text == "/errors":
                rows = db.execute(
                    "SELECT scope,error,created_at FROM incidents WHERE status='OPEN' ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                reply = "\n".join(
                    f"{datetime.fromtimestamp(r['created_at'], ZoneInfo('Asia/Shanghai')):%m-%d %H:%M:%S}"
                    f" · {r['scope']}：{r['error']}" for r in rows
                ) or "暂无分析异常"
                if get("analysis_paused", False):
                    reply = "分析已暂停；以下为尚未恢复验证的异常记录，不代表正在分析。\n" + reply
                if db.execute("SELECT 1 FROM outbox WHERE status='FAILED'").fetchone():
                    markup = {"inline_keyboard": [[{"text": "重试失败通知", "callback_data": "retry_delivery"}]]}
            elif text in ("/pause", "/resume"):
                if text == "/resume" and get("analysis_paused", False):
                    put("next_analysis_at", next_slot(time.time(), get("analysis_interval", 20)))
                put("analysis_paused", text == "/pause")
                reply = (
                    "已暂停后续分析，当前分析会完成。"
                    if text == "/pause"
                    else "已恢复分析；按配置频率在下一个固定周期时间点运行。"
                )
            elif text.startswith("/interval"):
                parts = text.split()
                if len(parts) == 2 and parts[1].isascii() and parts[1].isdigit() and 10 <= int(parts[1]) <= 1440 and int(parts[1]) % 10 == 0:
                    token = identity(update["update_id"], time.time())[:12]
                    put("interval_preview", {"minutes": int(parts[1]), "token": token, "expires": time.time() + 600})
                    put("interval_input", None)
                    reply = f"分析间隔将改为 {int(parts[1])} 分钟。保存后按固定周期时间点运行，当前分析不受影响。"
                    markup = {"inline_keyboard": [[
                        {"text": "✅ 保存", "callback_data": "confirm:" + token},
                        {"text": "取消", "callback_data": "cancel:" + token},
                    ]]}
                else:
                    token = identity(update["update_id"], time.time())[:12]
                    put("interval_preview", None)
                    put("interval_input", {"expires": time.time() + 600, "token": token})
                    reply = f"当前分析间隔 {get('analysis_interval', 20)} 分钟。\n请直接回复分钟数（10—1440，必须是 10 的整数倍），例如：20、120。"
                    if len(parts) > 1:
                        reply = "请输入 10—1440 之间且为 10 的整数倍的分钟数。\n" + reply
                    markup = {"inline_keyboard": [[{"text": "取消设置", "callback_data": "cancel:" + token}]]}
            elif text.startswith("/cancel "):
                token = text.split()[-1]
                matching = any(v and v.get("token") == token for v in (get("interval_preview"), get("interval_input")))
                if matching:
                    put("interval_preview", None)
                    put("interval_input", None)
                reply = "已取消设置，频率保持不变。" if matching else "这次设置已结束，请使用底部菜单。"
            elif text.startswith("/confirm "):
                preview = get("interval_preview")
                if (
                    preview and text.split()[-1] == preview["token"] and time.time() < preview["expires"]
                    and type(preview["minutes"]) is int
                    and 10 <= preview["minutes"] <= 1440 and preview["minutes"] % 10 == 0
                ):
                    due = next_slot(time.time(), preview["minutes"])
                    put("analysis_interval", preview["minutes"])
                    put("next_analysis_at", due)
                    put("interval_effective_at", due)
                    put("interval_preview", None)
                    put("interval_input", None)
                    db.execute("INSERT INTO events(created_at,kind,payload) VALUES (?,?,?)",
                               (time.time(), "INTERVAL_CHANGED", encode(preview)))
                    reply = f"✅ 已保存：每 {preview['minutes']} 分钟分析一次。" + ("当前保持暂停。" if get("analysis_paused", False) else "下一轮按固定周期时间点运行。")
                else:
                    reply = "这次设置已失效，请点击底部「分析频率」重新设置。"
            elif text == "/retry_delivery":
                db.execute(
                    "UPDATE outbox SET status='PENDING',attempts=0,next_attempt=0 WHERE status='FAILED'"
                )
                reply = "已重新排队失败的分析通知。"
            db.execute(
                "INSERT OR IGNORE INTO outbox(event_key,text,markup) VALUES (?,?,?)",
                ("command:" + str(update["update_id"]), reply, encode(markup) if markup else None),
            )

    async def poll(self):
        # Serialize offset read, request and command commit together.
        async with self.poll_lock:
            await self._poll()

    async def _poll(self):
        updates = await self.call(
            "getUpdates",
            {
                "offset": self.store.state("bot_offset", 0),
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        self.communication_ok("TELEGRAM_POLL")
        for update in updates:
            self.handle(update)
            callback = update.get("callback_query")
            if callback:
                await self.acknowledge_callback(callback)
            self.store.set("bot_offset", update["update_id"] + 1)

    def communication_failure(self, scope, error):
        now = time.time()
        key = scope + ":failure"
        failure = self.store.state(key) or {"since": now, "count": 0}
        failure["count"] += 1
        self.store.set(key, failure)
        # Persist safe diagnostics immediately, but alert only sustained outages.
        self.store.event(scope + "_FAILURE", {
            "error": str(error)[:500],
            "request": self.store.state(STATE_KEY) if scope == "TELEGRAM_POLL" else None,
        })
        conflict = isinstance(error, BotAPIError) and error.conflict
        if conflict or failure["count"] >= 3 and now - failure["since"] >= 120:
            self.store.incident(scope, "communication", {}, str(error)[:500])
        return min(60, 5 * 2 ** min(failure["count"] - 1, 4))

    def communication_ok(self, scope):
        self.store.set(scope + ":failure", None)
        self.store.set(scope + ":last_success", time.time())
        scopes = [scope]
        # Migrate only the old polling incident after actual successful polling.
        if scope == "TELEGRAM_POLL":
            old = self.store.rows(
                "SELECT error FROM incidents WHERE scope='ANALYSIS_BOT' AND status='OPEN'"
            )
            if old and "getUpdates" in old[0]["error"]:
                scopes.append("ANALYSIS_BOT")
        with self.store.connect() as db:
            for resolved in scopes:
                db.execute(
                    "UPDATE outbox SET status='CANCELLED' WHERE status IN ('PENDING','FAILED') "
                    "AND event_key IN (SELECT 'alert:' || incident_id FROM incidents WHERE scope=?)",
                    (resolved,),
                )
                db.execute(
                    "UPDATE incidents SET status='RESOLVED' WHERE scope=? AND status='OPEN'",
                    (resolved,),
                )

    def keyboard(self):
        paused = self.store.state("analysis_paused", False)
        return {"keyboard": [
            ["🧭 分析状态", "🧠 最近分析"],
            ["⏱ 分析频率", "⚠️ 分析异常"],
            ["▶️ 恢复分析" if paused else "⏸ 暂停分析"],
        ], "resize_keyboard": True, "is_persistent": False, "one_time_keyboard": False}

    async def acknowledge_callback(self, callback):
        message = callback.get("message", {})
        authorized = (
            message.get("chat", {}).get("id") == self.settings.telegram_chat_id
            and callback.get("from", {}).get("id") == self.settings.telegram_user_id
        )
        # UI failures must not replay committed commands or block later updates.
        try:
            await self.call("answerCallbackQuery", {
                "callback_query_id": callback["id"],
                "text": "" if authorized else "无权操作",
            })
        except RuntimeError:
            pass
    async def deliver(self):
        for r in self.store.rows(
            "SELECT * FROM outbox WHERE status='PENDING' AND next_attempt<=? ORDER BY rowid LIMIT 10",
            (time.time(),),
        ):
            if r["attempts"] >= 2:
                self.store.execute(
                    "UPDATE outbox SET status='FAILED' WHERE event_key=?", (r["event_key"],)
                )
                continue
            self.store.execute(
                "UPDATE outbox SET attempts=attempts+1 WHERE event_key=?", (r["event_key"],)
            )
            try:
                markup = json.loads(r["markup"] or "null")
                if r["event_key"].startswith("alert:"):
                    markup = {"inline_keyboard": [[{"text": "查看分析异常", "callback_data": "errors"}]]}
                reply = await self.call(
                    "sendMessage",
                    {
                        "chat_id": self.settings.telegram_chat_id,
                        "text": r["text"],
                        "reply_markup": markup if markup is not None else self.keyboard(),
                    },
                )
                self.communication_ok("TELEGRAM_DELIVERY")
                self.store.execute(
                    "UPDATE outbox SET status='SENT',message_id=? WHERE event_key=?",
                    (reply["message_id"], r["event_key"]),
                )
            except Exception:
                self.store.execute(
                    "UPDATE outbox SET next_attempt=? WHERE event_key=?",
                    (time.time() + 5, r["event_key"]),
                )
                raise

    async def close(self):
        try:
            if self.owns_poll_client:
                await self.poll_client.aclose()
            await self.client.aclose()
        finally:
            self.polling.close()
