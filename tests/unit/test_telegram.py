from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from bybit_signal.config import TelegramConfig
from bybit_signal.domain.enums import PriceType, SignalStrength, ToolStatus, TrackingStatus
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandidateAssessment,
    CanonicalPrice,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.notifications.telegram import TelegramBot, TelegramError
from bybit_signal.storage.sqlite import SignalStore


def _cycle() -> AnalysisCycleResult:
    now = datetime.now(UTC)
    conclusion = SignalConclusion(
        analysis_id="analysis_01",
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol="CYSUSDT",
            price_type=PriceType.LAST,
            value=Decimal("0.5"),
            timestamp=now,
        ),
        assessment=CandidateAssessment(
            symbol="CYSUSDT",
            strength=SignalStrength.NO_STRONG_SIGNAL,
            market_state="反弹观察",
            summary="当前没有经严格校验的强信号",
            details="短周期有反弹, 但证据冲突, 等待下一轮。",
        ),
        tracking_status=TrackingStatus.INDETERMINATE,
        comparison_with_previous="没有上一轮强信号。",
        tool_assessments=(
            ToolAssessment(
                tool="CMI",
                status=ToolStatus.PARTIAL,
                reason="one exchange is unavailable",
            ),
        ),
    )
    return AnalysisCycleResult(
        analysis_id="analysis_01",
        started_at=now,
        completed_at=now,
        candidate_symbols=("CYSUSDT",),
        conclusions=(conclusion,),
        strong_signal_count=0,
        context_sha256="a" * 64,
    )


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            key for child in value.values() for key in _all_keys(child)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {key for child in value for key in _all_keys(child)}
    return set()


class TelegramRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, payload))
        if method == "getMe":
            result: Any = {"id": 1, "is_bot": True, "username": "test_bot"}
        else:
            result = True if method == "answerCallbackQuery" else {"message_id": 10}
        return httpx.Response(200, json={"ok": True, "result": result})


async def _bot(tmp_path: Path, recorder: TelegramRecorder) -> tuple[TelegramBot, SignalStore]:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()
    client = httpx.AsyncClient(
        base_url="https://api.telegram.org",
        transport=httpx.MockTransport(recorder),
    )
    config = TelegramConfig(
        enabled=True,
        token="test-token",
        allowed_chat_ids=frozenset({123}),
        allowed_user_ids=frozenset({456}),
    )
    return TelegramBot(config, store, client=client), store


async def test_startup_reply_keyboard_has_required_non_removal_flags(
    tmp_path: Path,
) -> None:
    recorder = TelegramRecorder()
    bot, _ = await _bot(tmp_path, recorder)

    await bot.announce_started()

    _, payload = recorder.calls[-1]
    keyboard = payload["reply_markup"]
    assert keyboard["resize_keyboard"] is True
    assert keyboard["is_persistent"] is False
    assert keyboard["one_time_keyboard"] is False
    assert "remove_keyboard" not in _all_keys(payload)
    await bot.close()


async def test_cycle_delivery_is_idempotent_and_uses_inline_details(
    tmp_path: Path,
) -> None:
    recorder = TelegramRecorder()
    bot, store = await _bot(tmp_path, recorder)
    cycle = _cycle()
    await store.save_cycle(cycle, ())

    await bot.deliver_cycle(cycle)
    await bot.deliver_cycle(cycle)

    sends = [payload for method, payload in recorder.calls if method == "sendMessage"]
    assert len(sends) == 1
    assert "inline_keyboard" in sends[0]["reply_markup"]
    assert "remove_keyboard" not in _all_keys(sends[0])
    await bot.close()


async def test_callback_is_answered_before_details_are_sent(tmp_path: Path) -> None:
    recorder = TelegramRecorder()
    bot, store = await _bot(tmp_path, recorder)
    cycle = _cycle()
    await store.save_cycle(cycle, ())
    update = {
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 456},
            "message": {"chat": {"id": 123}},
            "data": "detail:analysis_01:CYSUSDT",
        }
    }

    await bot._handle_update(update)

    assert [method for method, _ in recorder.calls] == [
        "answerCallbackQuery",
        "sendMessage",
    ]
    assert "CYSUSDT 分析详情" in recorder.calls[1][1]["text"]
    await bot.close()


async def test_network_error_never_exposes_bot_token(tmp_path: Path) -> None:
    store = SignalStore(tmp_path / "signals.db")
    await store.initialize()

    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network failed")

    client = httpx.AsyncClient(
        base_url="https://api.telegram.org",
        transport=httpx.MockTransport(fail),
    )
    bot = TelegramBot(
        TelegramConfig(
            enabled=True,
            token="super-secret-token",
            allowed_chat_ids=frozenset({123}),
            allowed_user_ids=frozenset({456}),
        ),
        store,
        client=client,
    )

    with pytest.raises(TelegramError) as captured:
        await bot.preflight()

    assert "super-secret-token" not in str(captured.value)
    await bot.close()
