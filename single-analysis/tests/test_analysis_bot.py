import time
from unittest.mock import AsyncMock

import pytest

from analysis_core.bot import AnalysisBot
from analysis_core.config import Settings
from analysis_core.store import Store


@pytest.fixture
async def bot(tmp_path):
    instance = AnalysisBot(
        Settings(_env_file=None, telegram_token="test", telegram_chat_id=1, telegram_user_id=2),
        Store(tmp_path / "db"),
        client=AsyncMock(),
    )
    try:
        yield instance
    finally:
        await instance.close()


def update(i, text, user=2):
    return dict(update_id=i, message={"text": text, "chat": {"id": 1}, "from": {"id": user}})


def test_interval_requires_authorization_preview_and_confirmation(bot):
    bot.handle(update(1, "/interval 10", user=3))
    assert bot.store.state("interval_preview") is None
    bot.handle(update(2, "/interval 10"))
    assert bot.store.state("analysis_interval", 20) == 20
    token = bot.store.state("interval_preview")["token"]
    bot.handle(update(3, "/confirm " + token))
    assert bot.store.state("analysis_interval") == 10
    assert time.time() < bot.store.state("interval_effective_at") <= time.time() + 600
    bot.handle(update(3, "/interval 7"))
    assert bot.store.state("interval_preview") is None


def test_no_trade_controls_and_no_cross_bot_notifications(bot):
    bot.handle(update(1, "/status"))
    text = bot.store.rows("SELECT text FROM outbox")[0]["text"]
    assert "系统状态" in text and "保证金" not in text
    bot.handle(update(2, "/interval 0"))
    assert bot.store.state("interval_preview") is None


async def test_delivery_budget_survives_restart(bot):
    bot.store.queue("event", "analysis")
    bot.call = AsyncMock(side_effect=RuntimeError("offline"))
    for _ in range(2):
        bot.store.execute("UPDATE outbox SET next_attempt=0")
        with pytest.raises(RuntimeError):
            await bot.deliver()
    await bot.deliver()
    assert bot.call.await_count == 2
    bot.store.execute("UPDATE outbox SET next_attempt=0")
    await bot.deliver()
    assert bot.store.rows("SELECT status FROM outbox")[0]["status"] == "FAILED"


async def test_commands_are_replay_safe(bot):
    bot.handle(update(1, "/pause"))
    bot.handle(update(2, "/resume"))
    bot.handle(update(1, "/pause"))
    assert bot.store.state("analysis_paused") is False
    assert len(bot.store.rows("SELECT * FROM outbox")) == 2


def test_wrong_chat_and_expired_confirmation_cannot_change_frequency(bot):
    other = update(10, "/pause")
    other["message"]["chat"]["id"] = 99
    bot.handle(other)
    assert not bot.store.state("analysis_paused", False)
    bot.handle(update(11, "/interval 10"))
    preview = bot.store.state("interval_preview")
    preview["expires"] = time.time() - 1
    bot.store.set("interval_preview", preview)
    bot.handle(update(12, "/confirm " + preview["token"]))
    assert bot.store.state("analysis_interval", 20) == 20


async def test_poll_restart_deduplicates_updates_and_advances_offset(bot):
    batch = [update(100, "/pause"), update(101, "/resume")]
    bot.call = AsyncMock(return_value=batch)
    await bot.poll()
    assert bot.store.state("bot_offset") == 102
    assert bot.store.state("analysis_paused") is False
    await bot.poll()
    assert len(bot.store.rows("select * from outbox")) == 2
    assert bot.call.call_args.args[1]["offset"] == 102


async def test_manual_delivery_retry_is_explicit_and_success_not_resent(bot):
    bot.store.queue("failed", "test analysis notification")
    bot.store.execute("update outbox set status='FAILED',attempts=2")
    bot.handle(update(200, "/retry_delivery"))
    bot.call = AsyncMock(return_value={"message_id": 123})
    await bot.deliver()
    assert (
        bot.store.rows("select status from outbox where event_key='failed'")[0]["status"] == "SENT"
    )
    count = bot.call.await_count
    await bot.deliver()
    assert bot.call.await_count == count


async def test_http_error_never_leaks_token(bot):
    import httpx

    request = httpx.Request("POST", "https://api.telegram.org/botSECRET/sendMessage")
    bot.client.post.side_effect = httpx.ConnectError(
        "https://api.telegram.org/botSECRET/sendMessage", request=request
    )
    with pytest.raises(RuntimeError) as err:
        await bot.call("sendMessage")
    assert "SECRET" not in str(err.value)


async def test_failed_first_message_eventually_unblocks_following_messages(bot):
    bot.store.queue("first", "first")
    bot.store.queue("second", "second")

    async def call(method, payload):
        if payload["text"] == "first":
            raise RuntimeError("injected Telegram outage")
        return {"message_id": 88}

    bot.call = AsyncMock(side_effect=call)
    for _ in range(2):
        bot.store.execute("update outbox set next_attempt=0")
        with pytest.raises(RuntimeError):
            await bot.deliver()
    bot.store.execute("update outbox set next_attempt=0")
    await bot.deliver()
    statuses = {r["event_key"]: r["status"] for r in bot.store.rows("select * from outbox")}
    assert statuses == {"first": "FAILED", "second": "SENT"}
    assert bot.call.await_count == 3


def callback(i, data, user=2, chat=1, markup=None):
    return {"update_id": i, "callback_query": {
        "id": str(i), "from": {"id": user}, "data": data,
        "message": {"message_id": 77, "chat": {"id": chat}, "reply_markup": markup},
    }}


def test_details_keep_original_cycle_and_do_not_mutate_signals(bot):
    for cid, reason in (("old", "旧轮完整理由"), ("new", "新轮理由")):
        bot.store.claim_cycle(cid)
        bot.store.signal(cid, cid, "BTCUSDT", {})
        bot.store.signal_result(cid, "PUBLISHED", "LONG", {"reason": reason})
    before = bot.store.rows("SELECT * FROM signals")
    bot.handle(callback(1, "detail:old"))
    bot.handle(callback(1, "detail:old"))
    replies = bot.store.rows("SELECT * FROM outbox")
    assert len(replies) == 1
    assert "旧轮完整理由" in replies[0]["text"]
    assert "新轮理由" not in replies[0]["text"]
    assert bot.store.rows("SELECT * FROM signals") == before
    bot.handle(callback(2, "detail:missing"))
    assert "不存在" in bot.store.rows("SELECT text FROM outbox")[-1]["text"]


def test_callbacks_require_owner_and_chat(bot):
    bot.handle(callback(1, "pause", user=3))
    bot.handle(callback(2, "pause", chat=3))
    bot.handle(callback(3, "detail:missing", user=3))
    bot.handle(callback(4, "retry:unknown"))
    assert not bot.store.state("analysis_paused", False)
    assert bot.store.rows("SELECT * FROM outbox") == []


async def test_reply_keyboard_pause_resume_and_duplicate_clicks(bot):
    bot.handle(update(1, "⏸ 暂停分析"))
    assert bot.store.state("analysis_paused") is True
    assert bot.keyboard()["keyboard"][-1] == ["▶️ 恢复分析"]
    bot.handle(update(2, "⏸ 暂停分析"))
    assert bot.store.state("analysis_paused") is True
    bot.handle(update(3, "▶️ 恢复分析"))
    bot.handle(update(1, "⏸ 暂停分析"))
    assert bot.store.state("analysis_paused") is False
    assert bot.keyboard()["keyboard"][-1] == ["⏸ 暂停分析"]


async def test_delivery_uses_persisted_details_and_current_pause_state(bot):
    from analysis_core.notifications import cycle_keyboard, cycle_notice

    bot.store.claim_cycle("c")
    bot.store.signal("s", "c", "BTCUSDT", {})
    bot.store.signal_result("s", "PUBLISHED", "LONG", {"reason": "完整分析理由"})
    bot.store.queue("cycle:c", cycle_notice(bot.store, "c"), cycle_keyboard(bot.store, "c"))
    bot.store.set("analysis_paused", True)
    bot.call = AsyncMock(return_value={"message_id": 1})
    await bot.deliver()
    payload = bot.call.call_args.args[1]
    assert "完整分析理由" not in payload["text"]
    assert "BTCUSDT" in payload["text"]
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows[0] == [{"text": "📊 BTCUSDT", "callback_data": "detail:s"}]
    assert "keyboard" not in payload["reply_markup"]
    bot.store.claim_cycle("new")
    bot.store.signal("new", "new", "BTCUSDT", {})
    bot.store.signal_result("new", "PUBLISHED", "SHORT", {"reason": "另一轮理由"})
    bot.handle(callback(9, "detail:s"))
    detail = bot.store.rows("SELECT text FROM outbox WHERE event_key='command:9'")[0]["text"]
    assert "完整分析理由" in detail and "另一轮理由" not in detail
    assert all("📊" not in b for row in bot.keyboard()["keyboard"] for b in row)


async def test_callback_ui_failure_does_not_block_committed_command(bot):
    bot.call = AsyncMock(side_effect=[
        [callback(5, "pause")], RuntimeError("expired query"), RuntimeError("deleted message"),
    ])
    await bot.poll()
    assert bot.store.state("bot_offset") == 6
    assert bot.store.state("analysis_paused") is True
    assert len(bot.store.rows("SELECT * FROM outbox")) == 1


async def test_reply_keyboard_graybox_wire_and_restart(tmp_path):
    import json

    import httpx

    from analysis_core.notifications import cycle_keyboard, cycle_notice

    calls = []
    updates = []

    async def wire(request):
        method = request.url.path.rsplit('/', 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        if method == 'getUpdates':
            result = list(updates)
            updates.clear()
        elif method == 'sendMessage':
            markup = payload['reply_markup']
            assert ('keyboard' in markup) != ('inline_keyboard' in markup)
            if 'keyboard' in markup:
                assert all('📊' not in b for row in markup['keyboard'] for b in row)
            result = {'message_id': len(calls)}
        elif method == 'answerCallbackQuery':
            result = True
        else:
            pytest.fail('Unexpected Telegram method: ' + method)
        return httpx.Response(200, json={'ok':True, 'result':result})

    settings = Settings(_env_file=None,telegram_token='fixture',telegram_chat_id=1,telegram_user_id=2)
    store = Store(tmp_path/'db')
    store.claim_cycle('c')
    store.signal('s','c','BTCUSDT',{})
    store.signal_result('s','PUBLISHED','SHORT',{'reason':'完整的本轮分析理由'})
    store.queue('cycle:c',cycle_notice(store,'c'),cycle_keyboard(store,'c'))
    bot = AnalysisBot(settings,store,httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    await bot.deliver()
    await bot.close()
    restarted = AnalysisBot(settings,Store(store.path),httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    try:
        updates.append(callback(100,'detail:s'))
        await restarted.poll()
        await restarted.deliver()
        assert '完整的本轮分析理由' in calls[-1][1]['text']
        updates.append(update(101,'⏸ 暂停分析'))
        await restarted.poll()
        await restarted.deliver()
        assert calls[-1][1]['reply_markup']['keyboard'][-1] == ['▶️ 恢复分析']
        updates.extend([update(101,'⏸ 暂停分析'),update(102,'▶️ 恢复分析')])
        await restarted.poll()
        await restarted.deliver()
        assert calls[-1][1]['reply_markup']['keyboard'][-1] == ['⏸ 暂停分析']
        assert len([m for m,p in calls if m=='sendMessage']) == 4
        assert store.rows('SELECT side FROM signals') == [{'side':'SHORT'}]
    finally:
        await restarted.close()


async def test_navigation_contract_inline_edit_restart_and_legacy_queue(bot):
    import json

    import httpx

    sent = []

    async def wire(request):
        sent.append((request.url.path.rsplit('/', 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(sent)}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    bot.client = client

    def check(markup):
        assert markup['resize_keyboard'] is True
        assert markup['is_persistent'] is False
        assert markup['one_time_keyboard'] is False
        assert 'remove_keyboard' not in markup
        assert 'keyboard' in markup

    try:
        for i, text in enumerate(['🧭 分析状态', '⏸ 暂停分析', '▶️ 恢复分析']):
            bot.handle(update(500+i, text))
            await bot.deliver()
            check(sent[-1][1]['reply_markup'])
        bot.store.queue('legacy-nav', 'old', {'keyboard': [['old']], 'is_persistent': True,
                                             'one_time_keyboard': True})
        await bot.deliver()
        check(sent[-1][1]['reply_markup'])
        inline = {'inline_keyboard': [[{'text':'详情','callback_data':'detail:test'}]]}
        await bot.call('sendMessage', {'text': 'detail', 'reply_markup': inline})
        assert sent[-1][1]['reply_markup'] == inline
        await bot.call('editMessageText', {'text':'done', 'reply_markup':{'inline_keyboard':[]}})
        assert sent[-1][1]['reply_markup'] == {'inline_keyboard':[]}
        count = len(sent)
        restarted = AnalysisBot(bot.settings, Store(bot.store.path), client)
        await restarted.deliver()
        assert len(sent) == count
        restarted.handle(update(600, '⏱ 分析频率'))
        restarted.handle(update(601, '30'))
        token = bot.store.state('interval_preview')['token']
        restarted.handle(callback(602, 'confirm:'+token))
        await restarted.deliver()
        check(sent[-1][1]['reply_markup'])
        count = len(sent)
        with pytest.raises(ValueError, match='forbidden'):
            await bot.call('sendMessage', {'reply_markup': dict(remove_keyboard=True)})
        with pytest.raises(ValueError, match='inline'):
            await bot.call('editMessageReplyMarkup', {'reply_markup': bot.keyboard()})
        assert len(sent) == count
        assert all('remove_keyboard' not in str(payload) for _, payload in sent)
    finally:
        await client.aclose()


@pytest.mark.parametrize("minutes", ["0", "5", "15", "121", "1450", "-10", "10.0"])
def test_frequency_rejects_non_ten_minute_steps(bot, minutes):
    bot.handle(update(1, "⏱ 分析频率"))
    bot.handle(update(2, minutes))
    assert bot.store.state("interval_preview") is None
    assert bot.store.state("analysis_interval", 20) == 20
    assert "10 的整数倍" in bot.store.rows("SELECT text FROM outbox")[-1]["text"]


def test_old_non_multiple_preview_cannot_be_saved(bot):
    bot.store.set("interval_preview", {"minutes": 15, "token": "old", "expires": time.time() + 600})
    bot.handle(update(1, "/confirm old"))
    assert bot.store.state("analysis_interval", 20) == 20
    assert bot.store.state("next_analysis_at") is None


def test_status_shows_latest_round_and_shanghai_schedule(bot, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 0)
    bot.store.claim_cycle("latest")
    bot.store.execute("UPDATE cycles SET status='SUCCESS', completed_at=60")
    bot.store.event("SCREENING_RESULT", {"cycle_id": "latest", "selected": ["BTC", "ETH", "SOL"]})
    bot.store.set("analysis_interval", 120)
    bot.store.set("next_analysis_at", 7200)
    bot.handle(update(1, "🧭 分析状态"))
    text = bot.store.rows("SELECT text FROM outbox")[-1]["text"]
    for expected in ("系统状态", "服务状态: 运行中", "分析频率: 120 分钟",
                     "下次运行: 1970-01-01 10:00:00（Asia/Shanghai）",
                     "最后分析: latest", "完成时间: 1970-01-01 08:01:00（Asia/Shanghai）",
                     "本轮候选数: 3", "分析状态: SUCCESS", "Top10"):
        assert expected in text
    bot.store.set("analysis_paused", True)
    bot.handle(update(2, "/status"))
    text = bot.store.rows("SELECT text FROM outbox")[-1]["text"]
    assert "下次运行: 已暂停，恢复后重新安排" in text


async def test_paused_poll_recovers_old_incident_without_starting_analysis(bot):
    bot.store.set('analysis_paused', True)
    bot.store.incident('ANALYSIS_BOT', 'analysis', {}, 'getUpdates HTTP 409')
    bot.store.incident('MODEL_SERVICE', 'analysis', {}, 'historical capacity')
    bot.call = AsyncMock(return_value=[])
    await bot.poll()
    assert bot.store.state('analysis_paused') is True
    assert not bot.store.rows('SELECT * FROM cycles')
    incidents = {r['scope']: r['status'] for r in bot.store.rows('SELECT * FROM incidents')}
    assert incidents == {'ANALYSIS_BOT': 'RESOLVED', 'MODEL_SERVICE': 'OPEN'}
    assert bot.store.rows("SELECT status FROM outbox ORDER BY rowid")[0]['status'] == 'CANCELLED'
    assert bot.call.call_args.args[1]['timeout'] == 25


def test_transient_poll_failure_is_recorded_without_alert_and_sustained_alert_recovers(bot, monkeypatch):
    monkeypatch.setattr('analysis_core.bot.time.time', lambda: 1000)
    assert bot.communication_failure('TELEGRAM_POLL', RuntimeError('ReadTimeout')) == 5
    assert not bot.store.rows('SELECT * FROM incidents')
    assert len(bot.store.rows('SELECT * FROM events')) == 1
    monkeypatch.setattr('analysis_core.bot.time.time', lambda: 1060)
    bot.communication_failure('TELEGRAM_POLL', RuntimeError('ReadTimeout'))
    monkeypatch.setattr('analysis_core.bot.time.time', lambda: 1120)
    bot.communication_failure('TELEGRAM_POLL', RuntimeError('ReadTimeout'))
    alert = bot.store.rows('SELECT * FROM outbox')[0]
    assert '通信异常' in alert['text']
    assert '本轮受影响' not in alert['text']
    bot.communication_ok('TELEGRAM_POLL')
    assert bot.store.rows('SELECT status FROM incidents')[0]['status'] == 'RESOLVED'
    assert bot.store.rows('SELECT status FROM outbox')[0]['status'] == 'CANCELLED'


async def test_conflict_classified_without_echoing_sensitive_response(bot):
    import httpx

    from analysis_core.bot import BotAPIError

    bot.client.post.return_value = httpx.Response(409, json={
        'ok': False,
        'description': 'Conflict: terminated by other getUpdates request SECRET',
    })
    with pytest.raises(BotAPIError) as caught:
        await bot.poll()
    assert caught.value.conflict
    assert 'SECRET' not in str(caught.value)
    assert '其他getUpdates' in str(caught.value)
    bot.communication_failure('TELEGRAM_POLL', caught.value)
    assert bot.store.rows('SELECT status FROM incidents')[0]['status'] == 'OPEN'


def test_error_menu_labels_historical_time_and_pause(bot):
    bot.store.set('analysis_paused', True)
    bot.store.incident('MODEL_SERVICE', 'analysis', {}, 'capacity')
    bot.handle(update(900, '/errors'))
    text = bot.store.rows("SELECT text FROM outbox WHERE event_key='command:900'")[0]['text']
    assert '分析已暂停' in text
    assert '不代表正在分析' in text
    assert ' · MODEL_SERVICE' in text


def test_delivery_success_does_not_clear_poll_failure(bot):
    from analysis_core.bot import BotAPIError

    bot.communication_failure('TELEGRAM_POLL', BotAPIError('409', conflict=True))
    bot.communication_ok('TELEGRAM_DELIVERY')
    assert bot.store.rows('SELECT status FROM incidents')[0]['status'] == 'OPEN'
