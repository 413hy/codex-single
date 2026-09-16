"""User acceptance: menu -> reply -> save; resume near an old bucket boundary."""
import json

from test_analysis_bot import bot as bot_fixture
from test_analysis_bot import callback, update

from analysis_core.bot import AnalysisBot
from analysis_core.scheduling import claim_due, next_slot, reset_schedule
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


def test_resume_waits_for_fixed_slot_and_duplicate_resume_keeps_it(bot, monkeypatch):
    clock = [9*3600+17*60+20]
    monkeypatch.setattr('time.time', lambda: clock[0])
    bot.store.set('analysis_paused', True)
    bot.store.set('analysis_interval', 120)
    bot.handle(update(1, '▶️ 恢复分析'))
    assert bot.store.state('next_analysis_at') == 10*3600
    assert claim_due(bot.store, clock[0]) is None
    clock[0] += 60
    bot.handle(update(2, '▶️ 恢复分析'))
    assert bot.store.state('next_analysis_at') == 10*3600
    assert claim_due(Store(bot.store.path), 10*3600-1) is None
    first = claim_due(bot.store, 10*3600+0.5)
    assert first
    assert bot.store.state('next_analysis_at') == 12*3600
    bot.store.execute("UPDATE cycles SET status='SUCCESS'")
    assert claim_due(bot.store, 10*3600+1) is None
    assert claim_due(bot.store, 12*3600)
    bot.store.execute("UPDATE cycles SET status='SUCCESS'")
    assert claim_due(bot.store, 14*3600)


def test_pause_resume_during_running_cycle_does_not_add_immediate_round(bot, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1300)
    bot.store.claim_cycle('running')
    bot.handle(update(1, '⏸ 暂停分析'))
    bot.handle(update(2, '▶️ 恢复分析'))
    assert bot.store.state('next_analysis_at') == 2400
    assert claim_due(bot.store, 2400) is None


def test_frequency_save_uses_fixed_slots_and_skips_missed_slots(bot, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1000)
    bot.handle(update(1, '⏱ 分析频率'))
    bot.handle(update(2, '10'))
    token = bot.store.state('interval_preview')['token']
    bot.handle(callback(3, 'confirm:'+token))
    assert bot.store.state('next_analysis_at') == 1200
    assert claim_due(bot.store, 1199) is None
    assert claim_due(bot.store, 1200)
    bot.store.execute("UPDATE cycles SET status='SUCCESS'")
    assert claim_due(bot.store, 10000) is None
    assert bot.store.state('next_analysis_at') == 10200
    assert claim_due(bot.store, 10200)


def test_startup_migrates_relative_schedule_without_catchup(bot):
    bot.store.set('next_analysis_at', 1234)
    reset_schedule(bot.store, 1300)
    assert bot.store.state('next_analysis_at') == 2400
    assert claim_due(bot.store, 1300) is None
    assert claim_due(bot.store, 2400)


def test_initial_schedule_waits_and_slot_claim_is_durable(bot):
    assert claim_due(bot.store, 1000) is None
    assert bot.store.state('next_analysis_at') == 1200
    assert claim_due(bot.store, 1200)
    bot.store.execute("UPDATE cycles SET status='SUCCESS'")
    bot.store.set('next_analysis_at', 1200)
    assert claim_due(Store(bot.store.path), 1201) is None


def test_daily_slot_uses_shanghai_midnight_and_non_divisor_is_continuous():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 16, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    due = next_slot(now, 1440)
    assert datetime.fromtimestamp(due, ZoneInfo('Asia/Shanghai')).isoformat() == '2026-09-17T00:00:00+08:00'
    due = next_slot(now, 70)
    assert next_slot(due, 70) - due == 4200


def test_finishing_round_preserves_new_frequency_schedule(bot):
    bot.store.set('analysis_interval', 120)
    bot.store.set('next_analysis_at', 7200)
    reset_schedule(bot.store, 1300, only_overdue=True)
    assert bot.store.state('next_analysis_at') == 7200
    reset_schedule(bot.store, 7500, only_overdue=True)
    assert bot.store.state('next_analysis_at') == 14400


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
