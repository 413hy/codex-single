from __future__ import annotations

from typing import Any


def _bounded_callback(value: str) -> str:
    if not 1 <= len(value.encode("utf-8")) <= 64:
        raise ValueError("Telegram callback data must be 1-64 UTF-8 bytes")
    return value


def main_reply_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📈 最新信号"}, {"text": "🧭 系统状态"}],
            [{"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
        "input_field_placeholder": "查看最新信号或系统状态",
    }


def details_keyboard(analysis_id: str, symbol: str) -> dict[str, Any]:
    callback_data = _bounded_callback(f"detail:{analysis_id}:{symbol}")
    return {
        "inline_keyboard": [[{"text": "查看分析详情", "callback_data": callback_data}]],
    }


def cycle_details_keyboard(analysis_id: str, symbols: tuple[str, ...]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for rank, symbol in enumerate(symbols, start=1):
        callback_data = _bounded_callback(f"detail:{analysis_id}:{symbol}")
        rows.append(
            [
                {
                    "text": f"查看主信号 {rank} · {symbol}",
                    "callback_data": callback_data,
                }
            ]
        )
    return {"inline_keyboard": rows}


def back_to_cycle_keyboard(analysis_id: str) -> dict[str, Any]:
    callback_data = _bounded_callback(f"back:{analysis_id}")
    return {"inline_keyboard": [[{"text": "⬅️ 返回本轮信号", "callback_data": callback_data}]]}


def failure_keyboard(token: str, *, retry_enabled: bool = True) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if retry_enabled:
        rows.append(
            [
                {
                    "text": "🔄 按上述方案重试",
                    "callback_data": _bounded_callback(f"retry:{token}"),
                }
            ]
        )
    rows.append(
        [
            {
                "text": "📋 查看完整诊断链",
                "callback_data": _bounded_callback(f"failure:{token}"),
            }
        ]
    )
    return {"inline_keyboard": rows}


def back_to_failure_keyboard(
    token: str,
    *,
    retry_enabled: bool = True,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if retry_enabled:
        rows.append(
            [
                {
                    "text": "🔄 按上述方案重试",
                    "callback_data": _bounded_callback(f"retry:{token}"),
                }
            ]
        )
    rows.append(
        [
            {
                "text": "⬅️ 返回异常摘要",
                "callback_data": _bounded_callback(f"fback:{token}"),
            }
        ]
    )
    return {"inline_keyboard": rows}
