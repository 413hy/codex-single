from __future__ import annotations

from typing import Any


def main_reply_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📡 最新信号"}, {"text": "🩺 系统状态"}],
            [{"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
        "input_field_placeholder": "查看最新信号或系统状态",
    }


def details_keyboard(analysis_id: str, symbol: str) -> dict[str, Any]:
    callback_data = f"detail:{analysis_id}:{symbol}"
    if not 1 <= len(callback_data.encode("utf-8")) <= 64:
        raise ValueError("Telegram callback data must be 1-64 UTF-8 bytes")
    return {
        "inline_keyboard": [[{"text": "查看分析详情", "callback_data": callback_data}]],
    }
