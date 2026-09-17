import json

import httpx
import pytest

from longtime.config import Settings
from longtime.store import Store
from longtime.telegram import Telegram


@pytest.mark.parametrize("user,chat", [(1, 20), (10, 2)])
async def test_keyboard_control_rejects_non_owner(tmp_path, user, chat):
    s = Store(tmp_path / "db")
    bot = Telegram(Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20), s)
    try:
        await bot.handle(
            {
                "update_id": 1,
                "message": {"from": {"id": user}, "chat": {"id": chat}, "text": "▶️ 恢复开仓"},
            },
            None,
        )
        assert not s.rows("SELECT * FROM state")
        assert not s.rows("SELECT * FROM outbox")
    finally:
        await bot.close()


async def test_keyboard_pause_resume_delivery_and_views(tmp_path):
    s = Store(tmp_path / "db")
    sent = []

    async def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    bot = Telegram(
        Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20, trading_enabled=True),
        s,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        for i, text in enumerate(
            [
                "⏸️ 暂停开仓",
                "▶️ 恢复开仓",
                "📊 当前持仓",
                "🧭 运行状态",
                "🧾 最近交易",
                "🧠 最近分析",
                "⚠️ 异常处理",
                "⚙️ 策略说明",
            ]
        ):
            await bot.handle(
                {"update_id": i, "message": {"from": {"id": 10}, "chat": {"id": 20}, "text": text}},
                None,
            )
            await bot.deliver()
            assert sent[-1]["reply_markup"]["keyboard"]
            if i == 0:
                assert s.state("entries_paused") is True
                assert "▶️ 恢复开仓" in str(sent[-1]["reply_markup"])
            if i == 1:
                assert s.state("entries_paused") is False
                assert "⏸️ 暂停开仓" in str(sent[-1]["reply_markup"])
        assert len(sent) == 8
    finally:
        await bot.close()


def test_settlement_notice_explains_scope_without_internal_id(tmp_path):
    s = Store(tmp_path / "db")
    iid = s.incident(
        "SETTLEMENT:internal-trade-id", "monitor", {"symbol": "CASHCATUSDT"}, "raw diagnostic"
    )
    r = s.rows("SELECT * FROM outbox")[0]
    assert "CASHCATUSDT" in r["text"] and "收益待核对" in r["text"]
    assert "其他币照常处理" in r["text"] and "系统会继续核对" in r["text"]
    assert "SETTLEMENT:" not in r["text"] and "raw diagnostic" not in r["text"]
    assert json.loads(r["markup"])["inline_keyboard"][0][0]["callback_data"] == "retry:" + iid


def assert_compact_keyboard(markup):
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is False
    assert markup["one_time_keyboard"] is False
    assert len(markup["keyboard"]) == 3
    assert sum(len(row) for row in markup["keyboard"]) == 6
    assert "remove_keyboard" not in markup


async def test_navigation_inline_edit_and_restart_preserve_reply_keyboard(tmp_path):
    s = Store(tmp_path / "db")
    settings = Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20)
    sent = []

    async def handler(req):
        sent.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    def make_bot():
        return Telegram(
            settings,
            Store(tmp_path / "db"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    bot = make_bot()
    try:
        for i, text in enumerate(
            ["/start", "📊 当前持仓", "🧭 运行状态", "🧾 最近交易", "⏸️ 暂停开仓", "/strategy"]
        ):
            await bot.handle(
                {"update_id": i, "message": {"from": {"id": 10}, "chat": {"id": 20}, "text": text}},
                None,
            )
            await bot.deliver()
            assert_compact_keyboard(sent[-1][1]["reply_markup"])
        inline = {"inline_keyboard": [[{"text": "检查", "callback_data": "retry:test"}]]}
        s.queue("inline", "异常", inline)
        await bot.deliver()
        assert sent[-1][1]["reply_markup"] == inline
        await bot.call(
            "editMessageText",
            {
                "chat_id": 20,
                "message_id": 7,
                "text": "已解决",
                "reply_markup": {"inline_keyboard": []},
            },
        )
        assert sent[-1][1]["reply_markup"] == {"inline_keyboard": []}
        await bot.close()
        count = len(sent)
        bot = make_bot()
        await bot.deliver()
        assert len(sent) == count  # Restart neither removes nor needlessly reopens navigation.
        s.queue("after-restart", "服务消息")
        await bot.deliver()
        assert_compact_keyboard(sent[-1][1]["reply_markup"])
        assert "▶️ 恢复开仓" in str(sent[-1][1]["reply_markup"])
        assert all("remove_keyboard" not in str(payload) for _, payload in sent)
    finally:
        await bot.close()


async def test_old_queued_keyboard_normalized_and_removal_blocked(tmp_path):
    s = Store(tmp_path / "db")
    sent = []

    async def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = Telegram(
        Settings(_env_file=None), s, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        s.queue(
            "legacy",
            "旧菜单",
            {"keyboard": [["旧配置"]], "is_persistent": True, "one_time_keyboard": True},
        )
        await bot.deliver()
        assert_compact_keyboard(sent[-1]["reply_markup"])
        with pytest.raises(ValueError, match="forbidden"):
            await bot.call(
                "sendMessage", {"text": "bad", "reply_markup": dict(remove_keyboard=True)}
            )
        with pytest.raises(ValueError, match="inline"):
            await bot.call("editMessageReplyMarkup", {"reply_markup": {"keyboard": [["bad"]]}})
        assert len(sent) == 1
    finally:
        await bot.close()


async def test_recent_alerts_button_empty_and_unresolved_only(tmp_path):
    s = Store(tmp_path / "db")
    bot = Telegram(Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20), s)

    async def click(i):
        await bot.handle(
            {
                "update_id": i,
                "message": {"from": {"id": 10}, "chat": {"id": 20}, "text": "⚠️ 最近异常"},
            },
            None,
        )
        return s.rows("SELECT * FROM outbox WHERE event_key=?", ("command:" + str(i),))[0]

    try:
        assert (await click(1))["text"] == "✅ 最近无异常。"
        iid = s.incident("SL:test", "protection", {"symbol": "TESTUSDT"}, "failed")
        r = await click(2)
        assert "TESTUSDT" in r["text"]
        assert json.loads(r["markup"])["inline_keyboard"][0][0]["callback_data"] == "retry:" + iid
        s.resolve("SL:test")
        assert (await click(3))["text"] == "✅ 最近无异常。"
    finally:
        await bot.close()


@pytest.mark.parametrize("branch", ["unauthorized", "unknown", "duplicate", "database", "retry"])
@pytest.mark.parametrize("failure", ["400", "timeout"])
async def test_callback_ack_failure_consumes_update_and_unblocks_queue(tmp_path, branch, failure):
    import asyncio

    s = Store(tmp_path / "db")
    iid = s.incident("TEST", "monitor", {}, "failed")
    q = {
        "id": "callback",
        "from": {"id": 10},
        "message": {"chat": {"id": 20}},
        "data": "retry:" + iid,
    }
    if branch == "unauthorized":
        q["from"]["id"] = 99
    elif branch == "unknown":
        q["data"] = "retry:missing"
    elif branch == "duplicate":
        s.claim_callback(q["id"], iid)
        s.resolve("TEST")
    elif branch == "database":
        q["data"] = "retry:database"
    updates = [
        {"update_id": 100, "callback_query": q},
        {
            "update_id": 101,
            "message": {
                "from": {"id": 10},
                "chat": {"id": 20},
                "text": "/pause",
            },
        },
    ]
    offsets, acknowledgements, retries = [], [], []

    async def handler(req):
        method = req.url.path.rsplit("/", 1)[-1]
        payload = json.loads(req.content)
        if method == "getUpdates":
            offsets.append(payload["offset"])
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [u for u in updates if u["update_id"] >= payload["offset"]],
                },
            )
        assert method == "answerCallbackQuery"
        acknowledgements.append(payload)
        if failure == "timeout":
            raise httpx.ReadTimeout("token-bearing URL must not leak", request=req)
        return httpx.Response(400, json={"ok": False, "error_code": 400})

    async def retry(*args):
        retries.append(1)
        return True

    def make_bot():
        return Telegram(
            Settings(
                _env_file=None, runtime_dir=tmp_path, telegram_user_id=10, telegram_chat_id=20
            ),
            Store(tmp_path / "db"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    bot = make_bot()
    try:
        await bot.poll(retry)
        await asyncio.gather(*bot.tasks)
        assert s.state("telegram_offset") == 102
        assert s.state("entries_paused") is True
        assert retries == ([1] if branch == "retry" else [])
        await bot.close()
        bot = make_bot()
        await bot.poll(retry)
        assert offsets == [0, 102]
        assert len(acknowledgements) == 1
    finally:
        await bot.close()


async def test_poll_processing_failure_does_not_resolve_and_recreate_incident(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("longtime.telegram.time.monotonic", lambda: clock[0])
    s = Store(tmp_path / "db")
    iid = s.incident("TELEGRAM_POLL", "monitor", {}, "processing failed")
    bot = Telegram(Settings(_env_file=None), s)

    async def call(*args):
        return [{"update_id": 100}]

    async def handle(*args):
        raise RuntimeError("processing failed")

    bot.call, bot.handle = call, handle
    try:
        for _ in range(3):
            clock[0] += 31
            with pytest.raises(RuntimeError, match="processing failed"):
                await bot.poll(None)
            assert s.incident("TELEGRAM_POLL", "monitor", {}, "processing failed") == iid
        assert s.state("telegram_offset", 0) == 0
        assert len(s.rows("SELECT * FROM outbox")) == 1

        async def recovered(*args):
            return None

        bot.handle = recovered
        clock[0] += 31
        await bot.poll(None)
        assert s.state("telegram_offset") == 101
        assert s.rows("SELECT status FROM incidents")[0]["status"] == "OPEN"
        clock[0] += 300
        await bot.poll(None)
        assert s.rows("SELECT status FROM incidents")[0]["status"] == "RESOLVED"
    finally:
        await bot.close()


async def test_flapping_poll_keeps_one_alert_and_restart_does_not_prove_health(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("longtime.telegram.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("longtime.polling.time.time", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr("longtime.polling.asyncio.sleep", sleep)
    s = Store(tmp_path / "db")
    failing = [True]

    async def handler(request):
        if failing[0]:
            raise httpx.ReadTimeout("secret URL must not escape", request=request)
        return httpx.Response(200, json={"ok": True, "result": []})

    def make_bot():
        return Telegram(
            Settings(_env_file=None), s, httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    bot = make_bot()
    try:
        for _ in range(3):
            failing[0] = True
            clock[0] += 60
            with pytest.raises(RuntimeError, match=r"getUpdates failed \(ReadTimeout\)") as error:
                await bot.poll(None)
            s.incident("TELEGRAM_POLL", "monitor", {}, str(error.value))
            failing[0] = False
            clock[0] += 31
            await bot.poll(None)
        assert len(s.rows("SELECT * FROM incidents")) == 1
        assert len(s.rows("SELECT * FROM outbox")) == 1
        await bot.close()
        bot = make_bot()
        clock[0] += 1000
        await bot.poll(None)
        assert s.rows("SELECT status FROM incidents")[0]["status"] == "OPEN"
        clock[0] += 299
        await bot.poll(None)
        assert s.rows("SELECT status FROM incidents")[0]["status"] == "OPEN"
        clock[0] += 1
        await bot.poll(None)
        assert s.rows("SELECT status FROM incidents")[0]["status"] == "RESOLVED"
    finally:
        await bot.close()


@pytest.mark.parametrize(
    "scope,kind,error,expected,absent",
    [
        ("MODEL_SERVICE", "cycle", "GPT认证失败(401)", "认证失败", "容量不足"),
        ("MODEL_SERVICE", "cycle", "GPT分析超时(300秒)", "响应超时", "容量不足"),
        ("MODEL_SERVICE", "cycle", "capacity", "上游返回容量不足", "检查模型额度"),
        ("MODEL_SERVICE", "cycle", "GPT额度暂不可用", "额度不足", "容量不足"),
        ("ENTRY:rejected", "candidate", "110126 agreement required", "协议尚未签署", "该币已锁定"),
        ("ENTRY:rejected", "candidate", "10001 Qty invalid", "数量格式或精度", "结果待核对"),
        ("ENTRY:unknown", "reconcile", "response lost", "结果待核对", "本次请求未开仓"),
        ("TELEGRAM_POLL", "monitor", "ReadTimeout", "Bot指令可能延迟", "请查看当前持仓"),
    ],
)
def test_incident_text_explains_actual_failure_and_correct_retry(
    scope, kind, error, expected, absent
):
    from longtime.notices import incident_text

    text = incident_text(scope, kind, error, "12345678")
    assert expected in text and absent not in text


async def test_recent_alerts_show_original_occurrence_time(tmp_path):
    s = Store(tmp_path / "db")
    s.incident("MODEL_SERVICE", "cycle", {}, "capacity")
    s.execute("UPDATE incidents SET created_at=1789380000")
    bot = Telegram(Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20), s)
    try:
        await bot.handle(
            {
                "update_id": 11,
                "message": {"from": {"id": 10}, "chat": {"id": 20}, "text": "/alerts"},
            },
            None,
        )
        row = s.rows("SELECT text FROM outbox WHERE event_key='command:11'")[0]
        assert "发生时间：09-14 18:00:00" in row["text"]
    finally:
        await bot.close()
