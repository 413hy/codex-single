import json
from dataclasses import replace
from decimal import Decimal as D

import httpx
import pytest

from longtime.config import Settings
from longtime.settings_controls import PENDING
from longtime.store import Store
from longtime.telegram import Telegram
from longtime.trading_settings import SETTINGS_KEY, EntryDefaults


@pytest.fixture
async def bot(tmp_path):
    async def handler(req):
        return httpx.Response(200, json={"ok": True, "result": True})

    b = Telegram(
        Settings(_env_file=None, telegram_user_id=10, telegram_chat_id=20),
        Store(tmp_path / "db"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    yield b
    await b.close()


async def msg(bot, i, text, user=10):
    await bot.handle(
        {
            "update_id": i,
            "message": {"from": {"id": user}, "chat": {"id": 20}, "message_id": i, "text": text},
        },
        None,
    )


async def click(bot, i, data, user=10):
    await bot.handle(
        {
            "update_id": i,
            "callback_query": {
                "id": str(i),
                "from": {"id": user},
                "message": {"chat": {"id": 20}, "message_id": i - 1},
                "data": data,
            },
        },
        None,
    )


@pytest.mark.parametrize(
    "field,value", [("margin", "20"), ("leverage", "5"), ("tp", "1.5"), ("sl", "1")]
)
async def test_preview_save_restart_and_duplicate(bot, field, value):
    before = EntryDefaults.load(bot.store)
    await msg(bot, 1, "⚙️ 开仓设置")
    await click(bot, 2, "settings:edit:" + field)
    await msg(bot, 3, value)
    assert EntryDefaults.load(bot.store) == before
    pending = bot.store.state(PENDING)
    await click(bot, 4, "settings:save:" + pending["token"])
    result = EntryDefaults.load(Store(bot.store.path))
    assert getattr(result, field) == D(value) and result.revision == 1
    await click(bot, 4, "settings:save:" + pending["token"])
    assert len(bot.store.rows("SELECT * FROM events WHERE kind='ENTRY_DEFAULTS_CHANGED'")) == 1
    assert bot.store.state(PENDING) is None


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e9", "6", "2U"])
async def test_invalid_leverage_does_not_save(bot, value):
    await click(bot, 2, "settings:edit:leverage")
    await msg(bot, 3, value)
    assert "value" not in bot.store.state(PENDING)
    assert EntryDefaults.load(bot.store).leverage == 3


async def test_old_preview_cannot_save_new_value_and_cancel(bot):
    await click(bot, 2, "settings:edit:tp")
    await msg(bot, 3, "1")
    first = bot.store.state(PENDING)["token"]
    await msg(bot, 4, "2")
    await click(bot, 5, "settings:save:" + first)
    assert EntryDefaults.load(bot.store).revision == 0
    await msg(bot, 6, "/cancel")
    assert bot.store.state(PENDING) is None


async def test_unauthorized_edit_and_save_rejected(bot):
    await click(bot, 2, "settings:edit:sl", user=99)
    assert bot.store.state(PENDING) is None
    await click(bot, 3, "settings:edit:sl")
    await msg(bot, 4, "4", user=99)
    assert "value" not in bot.store.state(PENDING)
    await msg(bot, 5, "4")
    token = bot.store.state(PENDING)["token"]
    await click(bot, 6, "settings:save:" + token, user=99)
    assert EntryDefaults.load(bot.store).revision == 0


async def test_expiry_and_revision_conflict(bot):
    await click(bot, 2, "settings:edit:sl")
    await msg(bot, 3, "4")
    pending = bot.store.state(PENDING)
    pending["expires"] = 0
    bot.store.set(PENDING, pending)
    await click(bot, 4, "settings:save:" + pending["token"])
    assert EntryDefaults.load(bot.store).revision == 0
    await click(bot, 5, "settings:edit:sl")
    await msg(bot, 6, "4")
    pending = bot.store.state(PENDING)
    bot.store.set(SETTINGS_KEY, replace(EntryDefaults(), revision=1).document())
    await click(bot, 7, "settings:save:" + pending["token"])
    assert EntryDefaults.load(bot.store).sl == D("2.7")


async def test_entry_uses_saved_values_and_freezes_existing_trade(setup):
    e, s, x, m, d = setup
    cfg = EntryDefaults(margin=D(20), leverage=D(2), tp=D("1"), sl=D("3"), revision=1)
    s.set(SETTINGS_KEY, cfg.document())
    assert await e.enter("cycle", "sig", d) == "OPEN"
    t = s.rows("SELECT * FROM trades")[0]
    assert D(t["margin"]) == 20 and D(t["leverage"]) == 2 and D(t["qty"]) == D(".4")
    assert D(t["tp_target_net_pnl"]) == 1 and json.loads(t["details"])["sl_loss"] == "3"
    s.set(SETTINGS_KEY, EntryDefaults().document())
    await e.protect(t["trade_id"])
    assert s.trade(t["trade_id"])["sl_price"] == t["sl_price"]
    assert json.loads(s.trade(t["trade_id"])["details"])["entry_defaults"] == cfg.document()


async def test_configuration_change_during_account_read_skips_old_entry(setup):
    e, s, x, m, d = setup
    account = x.account

    async def changed():
        result = await account()
        s.set(SETTINGS_KEY, replace(EntryDefaults(), revision=1, tp=D(1)).document())
        return result

    x.account = changed
    assert await e.enter("cycle", "sig", d) == "SKIP_SETTINGS_CHANGED"
    assert not x.submissions


async def test_saved_margin_still_respects_available_funds(setup):
    e, s, x, m, d = setup
    s.set(SETTINGS_KEY, replace(EntryDefaults(), margin=D(90)).document())
    assert await e.enter("cycle", "sig", d) == "STOP_INSUFFICIENT_MARGIN"
    assert not x.submissions


async def test_notional_input_converts_margin_without_changing_leverage(bot):
    await click(bot, 2, "settings:edit:notional")
    await msg(bot, 3, "60")
    token = bot.store.state(PENDING)["token"]
    await click(bot, 4, "settings:save:" + token)
    cfg = EntryDefaults.load(bot.store)
    assert cfg.margin == 20 and cfg.leverage == 3
    assert cfg.margin * cfg.leverage == 60


async def test_save_transaction_rolls_back_on_outbox_failure(bot):
    await click(bot, 2, "settings:edit:sl")
    await msg(bot, 3, "4")
    token = bot.store.state(PENDING)["token"]
    bot.store.execute(
        "CREATE TRIGGER deny_settings BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await click(bot, 4, "settings:save:" + token)
    assert EntryDefaults.load(bot.store).revision == 0
    assert bot.store.state(PENDING)["token"] == token
    assert not bot.store.rows("SELECT * FROM events WHERE kind='ENTRY_DEFAULTS_CHANGED'")
    bot.store.execute("DROP TRIGGER deny_settings")
    await click(bot, 4, "settings:save:" + token)
    assert EntryDefaults.load(bot.store).sl == 4






def test_legacy_cycle_settings_are_ignored():
    legacy = EntryDefaults().document() | {"cycle_minutes": 5, "cycle_changed_at": 123}
    loaded = EntryDefaults.parse(legacy)
    assert "cycle_minutes" not in loaded.document()
    assert not hasattr(loaded, "cycle_minutes")
