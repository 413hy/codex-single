from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from bybit_signal.config import TelegramConfig
from bybit_signal.domain.enums import CycleMode, CycleStatus
from bybit_signal.domain.models import AnalysisCycleResult
from bybit_signal.notifications.formatter import (
    cycle_notification,
    detail_notification,
    emergency_notification,
    failure_notification,
)
from bybit_signal.notifications.keyboards import (
    back_to_cycle_keyboard,
    cycle_details_keyboard,
    details_keyboard,
    main_reply_keyboard,
)
from bybit_signal.storage.sqlite import SignalStore


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(
        self,
        config: TelegramConfig,
        store: SignalStore,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("Telegram bot cannot start when notifications are disabled")
        self._config = config
        self._store = store
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.api_base_url,
            timeout=httpx.Timeout(60, connect=15),
            headers={"User-Agent": "bybit-multi-source-signal/0.1"},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def preflight(self) -> dict[str, Any]:
        result = await self._api("getMe", {})
        if not isinstance(result, dict):
            raise TelegramError("Telegram getMe result is not an object")
        return cast(dict[str, Any], result)

    async def announce_started(self) -> None:
        for chat_id in self._config.allowed_chat_ids:
            await self.send_message(
                chat_id,
                "信号服务已启动。虚拟键盘可随时展开; 系统只发送人工判断用信号, 不会下单。",
                reply_markup=main_reply_keyboard(),
            )

    async def deliver_cycle(self, cycle: AnalysisCycleResult) -> None:
        if cycle.status is CycleStatus.FAILED:
            await self._deliver_failure(cycle)
            return
        await self._deliver_recovery_if_needed()
        if cycle.mode is CycleMode.SCHEDULED:
            text, symbols = cycle_notification(cycle)
            kind = "cycle"
            markup = cycle_details_keyboard(cycle.analysis_id, symbols)
        else:
            text, symbols = emergency_notification(cycle)
            kind = "emergency"
            markup = details_keyboard(cycle.analysis_id, symbols[0])
        for chat_id in self._config.allowed_chat_ids:
            if await self._store.delivery_exists(cycle.analysis_id, chat_id, kind):
                continue
            await self.send_message(chat_id, text, reply_markup=markup)
            await self._store.mark_delivered(
                cycle.analysis_id,
                chat_id,
                kind,
                datetime.now(UTC).isoformat(),
            )

    async def _deliver_failure(self, cycle: AnalysisCycleResult) -> None:
        text, code = failure_notification(cycle)
        state = f"FAILED:{code}"
        if await self._store.bot_state("analysis_health_state") == state:
            return
        for chat_id in self._config.allowed_chat_ids:
            await self.send_message(
                chat_id,
                text,
                reply_markup=main_reply_keyboard(),
            )
            await self._store.mark_delivered(
                cycle.analysis_id,
                chat_id,
                "failure",
                datetime.now(UTC).isoformat(),
            )
        await self._store.set_bot_state("analysis_health_state", state)

    async def _deliver_recovery_if_needed(self) -> None:
        previous = await self._store.bot_state("analysis_health_state")
        if previous is not None and previous.startswith("FAILED:"):
            for chat_id in self._config.allowed_chat_ids:
                await self.send_message(
                    chat_id,
                    "模型分析已经恢复, 本轮将继续发送两个主信号。",
                    reply_markup=main_reply_keyboard(),
                )
        if previous != "HEALTHY":
            await self._store.set_bot_state("analysis_health_state", "HEALTHY")

    async def poll_forever(self, stop_event: asyncio.Event) -> None:
        offset_value = await self._store.bot_state("telegram_update_offset")
        offset = int(offset_value) if offset_value is not None else 0
        while not stop_event.is_set():
            try:
                result = await self._api(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": self._config.polling_timeout_seconds,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                updates = result if isinstance(result, list) else []
                for value in updates:
                    if not isinstance(value, Mapping):
                        continue
                    update = cast(Mapping[str, Any], value)
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    await self._handle_update(update)
                    offset = max(offset, update_id + 1)
                    await self._store.set_bot_state("telegram_update_offset", str(offset))
            except TelegramError:
                await asyncio.sleep(3)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chunks = _message_chunks(text)
        result: dict[str, Any] = {}
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            value = await self._api("sendMessage", payload)
            result = value if isinstance(value, dict) else {}
        return result

    async def _handle_update(self, update: Mapping[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            await self._handle_callback(cast(Mapping[str, Any], callback))
            return
        message = update.get("message")
        if isinstance(message, Mapping):
            await self._handle_message(cast(Mapping[str, Any], message))

    async def _handle_message(self, message: Mapping[str, Any]) -> None:
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
            return
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        if not self._authorized(chat_id, user_id):
            return
        if text in {"/start", "/menu", "❓ 帮助"}:
            await self.send_message(
                chat_id,
                "可查看最新信号和系统状态。服务只分析公开行情并通知人工决策。",
                reply_markup=main_reply_keyboard(),
            )
        elif text in {"/latest", "📈 最新信号"}:
            latest = await self._store.latest_scheduled_cycle(successful_only=True)
            if latest is None:
                await self.send_message(
                    chat_id,
                    "还没有完成的分析周期。",
                    reply_markup=main_reply_keyboard(),
                )
            else:
                cycle_text, symbols = cycle_notification(latest)
                await self.send_message(
                    chat_id,
                    cycle_text,
                    reply_markup=cycle_details_keyboard(latest.analysis_id, symbols),
                )
        elif text in {"/status", "🧭 系统状态"}:
            status = await self._store.health_summary()
            await self.send_message(
                chat_id,
                "系统状态\n"
                f"最后分析: {status['last_analysis_id'] or '无'}\n"
                f"完成时间: {status['last_completed_at'] or '无'}\n"
                f"本轮主信号数: {status['selected_signal_count']}\n"
                f"模型状态: {status['model_status']}\n"
                f"最近成功周期: {status['last_successful_analysis_id'] or '无'}\n"
                f"最近紧急复核: {status['last_emergency_analysis_id'] or '无'}",
                reply_markup=main_reply_keyboard(),
            )

    async def _handle_callback(self, callback: Mapping[str, Any]) -> None:
        callback_id = callback.get("id")
        sender = callback.get("from")
        message = callback.get("message")
        data = callback.get("data")
        chat_id: int | None = None
        user_id: int | None = None
        if isinstance(sender, Mapping) and isinstance(sender.get("id"), int):
            user_id = int(sender["id"])
        if isinstance(message, Mapping):
            chat = message.get("chat")
            if isinstance(chat, Mapping) and isinstance(chat.get("id"), int):
                chat_id = int(chat["id"])
            message_id_value = message.get("message_id")
            message_id = (
                int(message_id_value) if isinstance(message_id_value, int) else None
            )
        else:
            message_id = None
        if not isinstance(callback_id, str):
            return
        if chat_id is None or user_id is None or not self._authorized(chat_id, user_id):
            await self._api(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "无权限"},
            )
            return
        await self._api("answerCallbackQuery", {"callback_query_id": callback_id})
        if not isinstance(data, str):
            return
        if data.startswith("back:"):
            analysis_id = data.split(":", 1)[1]
            cycle = await self._store.cycle(analysis_id)
            if cycle is None or cycle.status is CycleStatus.FAILED:
                await self.send_message(chat_id, "该分析已不存在或未成功完成。")
                return
            if cycle.mode is CycleMode.SCHEDULED:
                text, symbols = cycle_notification(cycle)
                markup = cycle_details_keyboard(cycle.analysis_id, symbols)
            else:
                text, symbols = emergency_notification(cycle)
                markup = details_keyboard(cycle.analysis_id, symbols[0])
            await self._edit_or_send(
                chat_id,
                message_id,
                text,
                markup,
            )
            return
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "detail":
            return
        conclusion = await self._store.conclusion(parts[1], parts[2])
        if conclusion is None:
            await self.send_message(chat_id, "该分析详情不存在或已清理。")
            return
        await self._edit_or_send(
            chat_id,
            message_id,
            detail_notification(conclusion),
            back_to_cycle_keyboard(conclusion.analysis_id),
        )

    async def _edit_or_send(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        reply_markup: dict[str, Any],
    ) -> None:
        if message_id is not None:
            try:
                await self._api(
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "link_preview_options": {"is_disabled": True},
                        "reply_markup": reply_markup,
                    },
                )
                return
            except TelegramError:
                pass
        await self.send_message(chat_id, text, reply_markup=reply_markup)

    def _authorized(self, chat_id: int, user_id: int) -> bool:
        return chat_id in self._config.allowed_chat_ids and user_id in self._config.allowed_user_ids

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                f"/bot{self._config.token}/{method}",
                json=payload,
            )
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TelegramError(f"Telegram {method} request failed") from error
        if response.status_code >= 400 or not isinstance(document, dict):
            raise TelegramError(f"Telegram {method} returned an invalid response")
        if document.get("ok") is not True:
            description = str(document.get("description") or "unknown Telegram error")
            raise TelegramError(f"Telegram {method} rejected the request: {description[:300]}")
        return document.get("result")


def _message_chunks(text: str, limit: int = 3900) -> tuple[str, ...]:
    if len(text) <= limit:
        return (text,)
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split = remaining.rfind("\n", 0, limit)
        if split < limit // 2:
            split = limit
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)
