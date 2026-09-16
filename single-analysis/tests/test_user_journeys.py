"""User acceptance: menu -> reply -> save; resume near an old bucket boundary."""
import json

from test_analysis_bot import bot as bot_fixture
from test_analysis_bot import callback, update

from analysis_core.bot import AnalysisBot
from analysis_core.scheduling import claim_due
from analysis_core.store import Store

bot = bot_fixture


def test_frequency_without_commands_survives_restart(bot):
    bot.handle(update(1, '⏱ 分析频率'))
    assert '直接回复' in bot.store.rows('SELECT text FROM outbox')[-1]['text']
    restarted = AnalysisBot(bot.settings, Store(bot.store.path), bot.client)
    restarted.handle(update(2, '10'))
    row = bot.store.rows('SELECT * FROM outbox')[-1]
    buttons = json.loads(row['markup'])['inline_keyboard'][0]
    assert [b['text'] for b in buttons] == ['✅ 保存', '取消']
    assert bot.store.state('analysis_interval', 20) == 20
    restarted.handle(callback(3, buttons[0]['callback_data']))
    assert bot.store.state('analysis_interval') == 10
    due = bot.store.state('next_analysis_at')
    restarted.handle(callback(4, buttons[0]['callback_data']))
    assert bot.store.state('next_analysis_at') == due


def test_input_validation_cancel_and_stale_buttons(bot):
    bot.handle(update(1, '⏱ 分析频率'))
    bot.handle(update(2, '0'))
    assert bot.store.state('interval_preview') is None
    bot.handle(update(3, '10'))
    old = bot.store.state('interval_preview')['token']
    bot.handle(update(4, '⏱ 分析频率'))
    current = bot.store.state('interval_input')['token']
    bot.handle(callback(5, 'cancel:' + old))
    assert bot.store.state('interval_input')['token'] == current
    bot.handle(update(6, '30', user=3))
    assert bot.store.state('interval_preview') is None
    bot.handle(update(7, '30'))
    token = bot.store.state('interval_preview')['token']
    bot.handle(callback(8, 'cancel:' + token))
    bot.handle(callback(9, 'confirm:' + token))
    assert bot.store.state('analysis_interval',20) == 20


def test_resume_at_0917_does_not_repeat_at_0920_or_restart(bot, monkeypatch):
    clock = [9*3600+17*60+20]
    monkeypatch.setattr('time.time', lambda: clock[0])
    bot.store.set('analysis_paused', True)
    bot.handle(update(1,'▶️ 恢复分析'))
    first = claim_due(bot.store, clock[0])
    assert first
    clock[0] = 9*3600+20*60
    assert claim_due(Store(bot.store.path), clock[0]) is None
    bot.handle(update(2,'▶️ 恢复分析'))
    assert claim_due(bot.store, clock[0]) is None
    clock[0] = 9*3600+37*60+20
    assert claim_due(bot.store, clock[0]) != first
    assert claim_due(bot.store, clock[0]) is None


def test_pause_resume_during_running_cycle_does_not_add_immediate_round(bot, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1000)
    assert claim_due(bot.store, 1000)
    bot.handle(update(1,'⏸ 暂停分析'))
    bot.handle(update(2,'▶️ 恢复分析'))
    assert bot.store.state('next_analysis_at') == 2200
    assert claim_due(bot.store, 1100) is None


def test_frequency_save_reschedules_and_downtime_does_not_catch_up(bot, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1000)
    bot.handle(update(1,'⏱ 分析频率'))
    bot.handle(update(2,'10'))
    token=bot.store.state('interval_preview')['token']
    bot.handle(callback(3,'confirm:'+token))
    assert claim_due(bot.store, 1599) is None
    assert claim_due(bot.store, 1600)
    assert claim_due(bot.store, 10000)
    assert claim_due(bot.store, 10001) is None


def test_legacy_migration_uses_last_actual_start(bot):
    bot.store.claim_cycle('legacy')
    bot.store.execute('UPDATE cycles SET started_at=1000')
    assert claim_due(bot.store, 1200) is None
    assert claim_due(bot.store, 2200)


def test_expired_input_and_navigation_exit_settings(bot, monkeypatch):
    clock = [1000]
    monkeypatch.setattr('time.time', lambda: clock[0])
    bot.handle(update(1,'⏱ 分析频率'))
    clock[0] += 601
    bot.handle(update(2,'10'))
    assert bot.store.state('interval_preview') is None
    bot.handle(update(3,'🧭 分析状态'))
    bot.handle(update(4,'10'))
    assert bot.store.state('interval_input') is None
    assert bot.store.state('analysis_interval',20) == 20


async def test_frequency_wire_uses_buttons_and_numeric_reply(bot):
    import httpx

    calls=[]
    async def wire(request):
        payload=json.loads(request.content)
        calls.append(payload)
        assert 'reply_markup' in payload
        return httpx.Response(200,json={'ok':True,'result':{'message_id':len(calls)}})
    client=httpx.AsyncClient(transport=httpx.MockTransport(wire))
    bot.client=client
    try:
        bot.handle(update(1,'⏱ 分析频率'))
        await bot.deliver()
        assert 'inline_keyboard' in calls[-1]['reply_markup']
        bot.handle(update(2,'30'))
        await bot.deliver()
        data=calls[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        bot.handle(callback(3,data))
        await bot.deliver()
        assert 'keyboard' in calls[-1]['reply_markup']
        assert bot.store.state('analysis_interval') == 30
        assert all('/confirm' not in p['text'] and '/interval' not in p['text'] for p in calls)
    finally:
        await client.aclose()
