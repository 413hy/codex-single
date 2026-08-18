from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from bybit_signal.config import TelegramConfig
from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    CycleStatus,
    Direction,
    MonitoringMetric,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandidateAssessment,
    CanonicalPrice,
    FormingHourOutlook,
    InvalidationCondition,
    MonitoringDirective,
    PriceLevel,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.notifications.formatter import cycle_notification, detail_notification
from bybit_signal.notifications.telegram import TelegramBot, TelegramError
from bybit_signal.storage.sqlite import SignalStore


def _cycle() -> AnalysisCycleResult:
    now = datetime.now(UTC)
    conclusions = tuple(
        _conclusion(
            symbol=symbol,
            rank=rank,
            price=price,
            now=now,
            strength=strength,
        )
        for symbol, rank, price, strength in (
            ("CYSUSDT", 1, Decimal("0.5"), SignalStrength.STRONG),
            ("GPSUSDT", 2, Decimal("0.2"), SignalStrength.WATCH),
        )
    )
    internal = SignalConclusion(
        analysis_id="analysis_01",
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol="ALICEUSDT",
            price_type=PriceType.LAST,
            value=Decimal("0.3"),
            timestamp=now,
        ),
        assessment=CandidateAssessment(
            symbol="ALICEUSDT",
            strength=SignalStrength.WATCH,
            market_state="internal watch",
            summary="not among the relative-best two signals",
            details="retained only for internal scheduled-cycle audit",
        ),
        tracking_status=TrackingStatus.INDETERMINATE,
        comparison_with_previous="no previous selected signal",
        tool_assessments=(
            ToolAssessment(tool="TEST", status=ToolStatus.AVAILABLE, reason="fixture"),
        ),
    )
    return AnalysisCycleResult(
        selection_contract_version=2,
        analysis_id="analysis_01",
        mode=CycleMode.SCHEDULED,
        status=CycleStatus.SUCCESS,
        started_at=now,
        completed_at=now,
        candidate_symbols=("CYSUSDT", "GPSUSDT", "ALICEUSDT"),
        conclusions=(*conclusions, internal),
        strong_signal_count=1,
        selected_symbols=("CYSUSDT", "GPSUSDT"),
        selected_signal_count=2,
        context_sha256="a" * 64,
    )


def _conclusion(
    *,
    symbol: str,
    rank: int,
    price: Decimal,
    now: datetime,
    strength: SignalStrength,
) -> SignalConclusion:
    evidence_id = f"{symbol}.PA.5M"
    target = price * Decimal("1.02")
    invalidation = price * Decimal("0.98")
    observed = now.replace(second=0, microsecond=0)
    forming_15m_start = observed.replace(minute=observed.minute - observed.minute % 15)
    forming_30m_start = observed.replace(minute=observed.minute - observed.minute % 30)
    forming_1h_start = observed.replace(minute=0)

    def outlook(start: datetime, duration: timedelta) -> FormingHourOutlook:
        return FormingHourOutlook(
            direction=Direction.LONG_BIAS,
            strength="NORMAL",
            window_start=start,
            window_end=start + duration,
            rationale="completed multi-timeframe structure supports this window",
        )

    assessment = CandidateAssessment(
        symbol=symbol,
        strength=strength,
        selection_rank=rank,  # type: ignore[arg-type]
        confidence=SignalConfidence.MEDIUM,
        direction=Direction.LONG_BIAS,
        market_state="near-term trend continuation",
        take_profit=PriceLevel(
            value=target,
            rationale="nearby completed structure",
            evidence_ids=(evidence_id,),
        ),
        forming_15m=outlook(forming_15m_start, timedelta(minutes=15)),
        forming_30m=outlook(forming_30m_start, timedelta(minutes=30)),
        forming_1h=outlook(forming_1h_start, timedelta(hours=1)),
        next_15m=outlook(
            forming_15m_start + timedelta(minutes=15),
            timedelta(minutes=15),
        ),
        invalidation=InvalidationCondition(
            condition="completed 5m closes below the structure",
            reference_price=invalidation,
            evidence_ids=(evidence_id,),
        ),
        summary="relative-best long signal for manual review",
        details="5m and 15m structure support the near-term direction",
        evidence_ids=(evidence_id,),
        monitoring_directives=(
            MonitoringDirective(
                family_id=f"{symbol.lower()}.invalidation.5m",
                metric=MonitoringMetric.COMPLETED_5M_CLOSE,
                comparator=Comparator.LESS_THAN,
                threshold=invalidation,
                hysteresis=Decimal("0.001"),
                valid_for_seconds=3600,
                reason="completed close invalidates the selected signal",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    return SignalConclusion(
        analysis_id="analysis_01",
        generated_at=now,
        canonical_price=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=price,
            timestamp=now,
        ),
        assessment=assessment,
        tracking_status=TrackingStatus.NEW,
        comparison_with_previous="new relative-best signal",
        tool_assessments=(
            ToolAssessment(
                tool="TEST",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence available",
                evidence_ids=(evidence_id,),
            ),
        ),
    )


def _failed_cycle(analysis_id: str = "analysis_fail_01") -> AnalysisCycleResult:
    now = datetime.now(UTC)
    return AnalysisCycleResult(
        selection_contract_version=2,
        analysis_id=analysis_id,
        mode=CycleMode.SCHEDULED,
        status=CycleStatus.FAILED,
        started_at=now,
        completed_at=now,
        candidate_symbols=("CYSUSDT", "GPSUSDT"),
        conclusions=(),
        strong_signal_count=0,
        selected_symbols=(),
        selected_signal_count=0,
        context_sha256="f" * 64,
        diagnostics={"failures": {"CODEX": "CODEX_AUTH_REQUIRED: login required"}},
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
    assert len(sends[0]["reply_markup"]["inline_keyboard"]) == 2
    assert "ALICEUSDT" not in sends[0]["text"]
    assert "WATCH" not in sends[0]["text"]
    assert "形成中 15m (分析时)" in sends[0]["text"]
    assert "形成中 30m (分析时)" in sends[0]["text"]
    assert "形成中 1h (分析时)" in sends[0]["text"]
    assert "下一根 15m" in sends[0]["text"]
    assert "remove_keyboard" not in _all_keys(sends[0])
    await bot.close()


def test_long_cycle_and_detail_keep_required_outlooks_and_valid_html() -> None:
    cycle = _cycle()
    conclusions = tuple(
        conclusion.model_copy(
            update={
                "assessment": conclusion.assessment.model_copy(
                    update={
                        "summary": "高波动结构说明" * 120,
                        "details": "完整结构证据与反证说明" * 1000,
                    }
                )
            }
        )
        if conclusion.assessment.selection_rank is not None
        else conclusion
        for conclusion in cycle.conclusions
    )
    long_cycle = cycle.model_copy(update={"conclusions": conclusions})

    text, _ = cycle_notification(long_cycle)
    detail = detail_notification(conclusions[0])

    for value in (text, detail):
        assert len(value) <= 3900
        assert value.count("<b>") == value.count("</b>")
        assert value.count("<code>") == value.count("</code>")
    for marker in ("形成中 15m", "形成中 30m", "形成中 1h", "下一根 15m"):
        assert marker in text
        assert marker in detail


async def test_callback_is_answered_before_details_are_sent(tmp_path: Path) -> None:
    recorder = TelegramRecorder()
    bot, store = await _bot(tmp_path, recorder)
    cycle = _cycle()
    await store.save_cycle(cycle, ())
    update = {
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 456},
            "message": {"message_id": 10, "chat": {"id": 123}},
            "data": "detail:analysis_01:CYSUSDT",
        }
    }

    await bot._handle_update(update)

    assert [method for method, _ in recorder.calls] == [
        "answerCallbackQuery",
        "editMessageText",
    ]
    assert "CYSUSDT 分析详情" in recorder.calls[1][1]["text"]
    assert recorder.calls[1][1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ] == "back:analysis_01"

    back = {
        "callback_query": {
            "id": "callback-2",
            "from": {"id": 456},
            "message": {"message_id": 10, "chat": {"id": 123}},
            "data": "back:analysis_01",
        }
    }
    await bot._handle_update(back)

    assert [method for method, _ in recorder.calls[-2:]] == [
        "answerCallbackQuery",
        "editMessageText",
    ]
    assert "本轮主信号: <b>2</b> / 2" in recorder.calls[-1][1]["text"]
    await bot.close()


async def test_failure_notice_is_deduplicated_and_recovery_is_announced(
    tmp_path: Path,
) -> None:
    recorder = TelegramRecorder()
    bot, store = await _bot(tmp_path, recorder)
    failed = _failed_cycle()
    await store.save_cycle(failed, ())

    await bot.deliver_cycle(failed)
    await bot.deliver_cycle(failed.model_copy(update={"analysis_id": "analysis_fail_02"}))
    await bot.deliver_cycle(_cycle())

    sends = [payload for method, payload in recorder.calls if method == "sendMessage"]
    assert len(sends) == 3
    assert "信号分析暂不可用" in sends[0]["text"]
    assert "模型分析已经恢复" in sends[1]["text"]
    assert "本轮主信号: <b>2</b> / 2" in sends[2]["text"]
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
