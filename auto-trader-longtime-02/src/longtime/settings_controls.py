"""Transactional Telegram edit/preview/save flow for entry defaults."""

import json
import re
import time
from dataclasses import replace
from decimal import ROUND_DOWN
from decimal import Decimal as D

from longtime.store import encode, identity
from longtime.trading_settings import SETTINGS_KEY, EntryDefaults

LABELS = {
    "margin": "每笔保证金",
    "leverage": "默认杠杆",
    "tp": "净止盈",
    "sl": "对冲距离",
    "notional": "每笔总价值",
}
PENDING = "entry_settings_editor"


def panel(defaults):
    return "⚙️ 开仓设置\n\n" + defaults.description(), {
        "inline_keyboard": [
            [
                {"text": "💰 每笔保证金", "callback_data": "settings:edit:margin"},
                {"text": "💵 每笔总价值", "callback_data": "settings:edit:notional"},
            ],
            [
                {"text": "⚡ 杠杆", "callback_data": "settings:edit:leverage"},
            ],
            [
                {"text": "🎯 默认止盈", "callback_data": "settings:edit:tp"},
                {"text": "🛑 默认对冲", "callback_data": "settings:edit:sl"},
            ],
        ]
    }


def candidate_for(current, field, value):
    if field == "cycle_minutes":
        raise ValueError("分析频率已迁移至分析Bot，交易系统不再设置周期")
    if field == "notional":
        return replace(
            current,
            margin=(value / current.leverage).quantize(D("0.00000001"), rounding=ROUND_DOWN),
        )
    return replace(current, **{field: value})


def process(store, event_id, *, action=None, text=None, actor=None, message_id=None):
    """Auth is checked by Telegram first; state, audit and reply commit together."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "INSERT OR IGNORE INTO callbacks VALUES (?,?)", ("settings:" + event_id, time.time())
        ).rowcount:
            return

        def state(key):
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

        def set_state(key, value):
            db.execute(
                "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encode(value)),
            )

        current = EntryDefaults.parse(state(SETTINGS_KEY))
        pending = state(PENDING)
        if pending and pending.get("field") == "cycle_minutes":
            set_state(PENDING, None)
            pending = None
        markup = None
        if action == "view":
            set_state(PENDING, None)
            reply, markup = panel(current)
        elif action and action.startswith("edit:") and action[5:] in LABELS:
            field = action[5:]
            token = identity(event_id, field, time.time())[:16]
            set_state(
                PENDING,
                {
                    "field": field,
                    "token": token,
                    "revision": current.revision,
                    "expires": time.time() + 600,
                    "after_message_id": message_id or 0,
                },
            )
            unit = "倍，范围1—5" if field == "leverage" else "U，填写正数，不加单位"
            old_value = (
                current.margin * current.leverage
                if field == "notional"
                else getattr(current, field)
            )
            reply = f"请输入{LABELS[field]}（{unit}）。当前值：{old_value:f}\n"
            if field == "margin":
                reply += "这里是每笔保证金；开仓总价值≈保证金×杠杆。\n"
            if field == "notional":
                reply += "这里是杠杆后的单笔名义金额，将按当前杠杆换算保证金（向下保留8位小数）。\n"
            if field == "leverage":
                reply += "修改杠杆保留每笔保证金，因此预计总价值会变化。\n"
            reply += "输入后会先展示预览，点击保存才生效；10分钟内有效。"
            markup = {
                "inline_keyboard": [[{"text": "取消", "callback_data": "settings:cancel:" + token}]]
            }
        elif action and action.startswith(("save:", "cancel:")):
            verb, token = action.split(":", 1)
            if not pending or token != pending["token"]:
                reply = "该设置操作已结束，请重新打开开仓设置。"
            elif verb == "cancel":
                set_state(PENDING, None)
                reply = "已取消，开仓配置未修改。"
            elif pending["expires"] < time.time() or pending["revision"] != current.revision:
                set_state(PENDING, None)
                reply = "预览已过期或配置已变化，请重新打开开仓设置。"
            elif "value" not in pending:
                reply = "请先输入数值。"
            else:
                updated = replace(
                    candidate_for(current, pending["field"], D(pending["value"])),
                    revision=current.revision + 1,
                )
                updated = EntryDefaults.parse(updated.document())
                set_state(SETTINGS_KEY, updated.document())
                set_state(PENDING, None)
                db.execute(
                    "INSERT INTO events(created_at,kind,payload) VALUES (?,?,?)",
                    (
                        time.time(),
                        "ENTRY_DEFAULTS_CHANGED",
                        encode(
                            {
                                "before": current.document(),
                                "after": updated.document(),
                                "actor": actor,
                                "event_id": event_id,
                            }
                        ),
                    ),
                )
                reply = "✅ 已保存，无需重启。\n\n" + updated.description()
        elif text is not None and pending:
            if text == "/cancel":
                set_state(PENDING, None)
                reply = "已取消，开仓配置未修改。"
            elif message_id and message_id <= pending.get("after_message_id", 0):
                reply = "这条输入早于当前设置操作，请重新输入。"
            elif pending["expires"] < time.time() or pending["revision"] != current.revision:
                set_state(PENDING, None)
                reply = "设置输入已过期，请重新打开开仓设置。"
            elif not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,8})?", text):
                reply = "请输入正数（最多8位小数），不要附带单位；或发送 /cancel 取消。"
            else:
                try:
                    candidate = candidate_for(current, pending["field"], D(text))
                    EntryDefaults.parse(candidate.document())
                except ValueError as error:
                    reply = str(error) + "；请重新输入。"
                else:
                    # Every preview has a fresh token; older save buttons cannot save newer input.
                    pending.update(value=text, token=identity(event_id, text)[:16])
                    set_state(PENDING, pending)
                    reply = "请确认新配置（尚未生效）：\n\n" + candidate.description()
                    markup = {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "✅ 保存",
                                    "callback_data": "settings:save:" + pending["token"],
                                },
                                {
                                    "text": "取消",
                                    "callback_data": "settings:cancel:" + pending["token"],
                                },
                            ]
                        ]
                    }
        else:
            return
        db.execute(
            "INSERT OR IGNORE INTO outbox(event_key,text,markup) VALUES (?,?,?)",
            ("settings:" + event_id, reply, encode(markup) if markup else None),
        )
