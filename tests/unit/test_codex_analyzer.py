from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bybit_signal.analysis.codex import (
    CodexAnalysisError,
    CodexAnalyzer,
    CodexProcessResult,
    _candle_outlook_windows,
)
from bybit_signal.config import AnalysisConfig
from bybit_signal.domain.enums import CycleMode, PriceType, SignalStrength, ToolStatus
from bybit_signal.domain.models import (
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    ToolAssessment,
)

TEST_NOW = datetime(2026, 8, 18, 3, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("requested_at", "expected"),
    [
        (
            datetime(2026, 8, 18, 3, 0, tzinfo=UTC),
            {
                "forming_15m": ("03:00", "03:15"),
                "forming_30m": ("03:00", "03:30"),
                "forming_1h": ("03:00", "04:00"),
                "next_15m": ("03:15", "03:30"),
            },
        ),
        (
            datetime(2026, 8, 18, 3, 29, 59, tzinfo=UTC),
            {
                "forming_15m": ("03:15", "03:30"),
                "forming_30m": ("03:00", "03:30"),
                "forming_1h": ("03:00", "04:00"),
                "next_15m": ("03:30", "03:45"),
            },
        ),
        (
            datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC),
            {
                "forming_15m": ("23:45", "00:00"),
                "forming_30m": ("23:30", "00:00"),
                "forming_1h": ("23:00", "00:00"),
                "next_15m": ("00:00", "00:15"),
            },
        ),
    ],
)
def test_candle_outlook_windows_follow_natural_boundaries(
    requested_at: datetime,
    expected: dict[str, tuple[str, str]],
) -> None:
    actual = {
        name: (start.strftime("%H:%M"), end.strftime("%H:%M"))
        for name, (start, end) in _candle_outlook_windows(requested_at).items()
    }

    assert actual == expected


def _bundle(symbol: str = "CYSUSDT", price: str = "100") -> EvidenceBundle:
    now = datetime.now(UTC)
    evidence = EvidenceItem(
        evidence_id=f"{symbol}.PA.5M",
        category="price_action",
        source="TEST",
        observed_at=now,
        summary="completed candles",
        values={"latest_close": 100.0},
    )
    return EvidenceBundle(
        symbol=symbol,
        generated_at=now,
        source_snapshot_sha256="a" * 64,
        canonical_last=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.LAST,
            value=Decimal(price),
            timestamp=now,
        ),
        canonical_mark=CanonicalPrice(
            symbol=symbol,
            price_type=PriceType.MARK,
            value=Decimal(price) + Decimal("0.1"),
            timestamp=now,
        ),
        evidence_items=(evidence,),
        tool_assessments=(
            ToolAssessment(
                tool="TEST_EVIDENCE",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence is available",
                evidence_ids=(evidence.evidence_id,),
            ),
        ),
    )


def _regime_bundle(
    five_values: dict[str, Any],
    fifteen_values: dict[str, Any],
) -> EvidenceBundle:
    bundle = _bundle()
    now = bundle.generated_at
    items = (
        EvidenceItem(
            evidence_id="CYSUSDT.PA.5M",
            category="price_action",
            source="TEST",
            observed_at=now,
            summary="completed 5m regime evidence",
            values=five_values,
        ),
        EvidenceItem(
            evidence_id="CYSUSDT.PA.15M",
            category="price_action",
            source="TEST",
            observed_at=now,
            summary="completed 15m regime evidence",
            values=fifteen_values,
        ),
    )
    latest = Decimal(str(five_values["latest_close"]))
    return bundle.model_copy(
        update={
            "canonical_last": bundle.canonical_last.model_copy(update={"value": latest}),
            "canonical_mark": bundle.canonical_mark.model_copy(
                update={"value": latest + Decimal("0.00001")}
            ),
            "evidence_items": items,
        }
    )


def _assessment(
    *,
    symbol: str = "CYSUSDT",
    evidence_id: str = "CYSUSDT.PA.5M",
    strength: str = "NO_STRONG_SIGNAL",
    selection_rank: int | None = None,
    direction: str | None = "LONG_BIAS",
    target: float | None = 103,
    invalidation: float | None = 97,
    visible: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbol": symbol,
        "strength": strength,
        "selection_rank": selection_rank,
        "confidence": "MEDIUM" if selection_rank is not None else None,
        "direction": direction,
        "market_state": "测试市场状态",
        "take_profit": None,
        "forming_15m": None,
        "forming_30m": None,
        "forming_1h": None,
        "next_15m": None,
        "invalidation": None,
        "summary": "当前没有足够强的交易机会",
        "details": "已检查输入证据, 当前不满足强信号条件。",
        "uncertainties": [],
        "evidence_ids": [evidence_id],
        "monitoring_directives": [],
    }
    if strength == "STRONG" or selection_rank is not None or visible:
        result["take_profit"] = {
            "value": target,
            "rationale": "已完成K线结构目标",
            "evidence_ids": [evidence_id],
        }
        result["forming_1h"] = {
            "direction": direction,
            "strength": "NORMAL",
            "window_start": "2026-08-17T14:00:00Z",
            "window_end": "2026-08-17T15:00:00Z",
            "rationale": "短周期结构延续",
        }
        result["invalidation"] = {
            "condition": "价格稳定突破反方向结构位",
            "reference_price": invalidation,
            "evidence_ids": [evidence_id],
        }
        _forming_now(result)
    if selection_rank is not None:
        comparator = "LESS_THAN" if direction == "LONG_BIAS" else "GREATER_THAN"
        result["monitoring_directives"] = [
            {
                "family_id": f"{symbol.lower()}.invalidation.5m",
                "metric": "COMPLETED_5M_CLOSE",
                "comparator": comparator,
                "threshold": invalidation,
                "hysteresis": 0.01,
                "valid_for_seconds": 3600,
                "reason": "完成柱越过失效结构后重新分析",
                "evidence_ids": [evidence_id],
            }
        ]
    return result


def _response(
    *assessments: dict[str, Any],
    mode: str = "EMERGENCY",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "analysis_id": "analysis_01",
        "cycle_summary": "本轮完成一个币种的独立判断。",
        "assessments": list(assessments),
    }


def _forming_now(assessment: dict[str, Any]) -> dict[str, Any]:
    direction = assessment["direction"]
    for name, (start, end) in _candle_outlook_windows(TEST_NOW).items():
        assessment[name] = {
            "direction": direction,
            "strength": "NORMAL",
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "rationale": f"{name} completed-structure forecast",
        }
    return assessment


def _second_selected() -> tuple[EvidenceBundle, dict[str, Any]]:
    return (
        _bundle("GPSUSDT"),
        _forming_now(
            _assessment(
                symbol="GPSUSDT",
                evidence_id="GPSUSDT.PA.5M",
                strength="WATCH",
                selection_rank=2,
                direction="LONG_BIAS",
                target=103,
                invalidation=97,
            )
        ),
    )


class FakeCodexRunner:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.commands: list[tuple[str, ...]] = []
        self.prompts: list[str] = []

    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        stdin: str,
    ) -> CodexProcessResult:
        del cwd, timeout_seconds
        command_tuple = tuple(command)
        self.commands.append(command_tuple)
        self.prompts.append(stdin)
        output = Path(
            command_tuple[command_tuple.index("--output-last-message") + 1]
        )
        response = self.responses[min(len(self.commands) - 1, len(self.responses) - 1)]
        output.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        event = {"usage": {"input_tokens": 100, "output_tokens": 20}}
        return CodexProcessResult(0, json.dumps(event), "", 5)


class FailingCodexRunner:
    def __init__(self, stderr: str) -> None:
        self.stderr = stderr
        self.calls = 0

    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        stdin: str,
    ) -> CodexProcessResult:
        del command, cwd, timeout_seconds, stdin
        self.calls += 1
        return CodexProcessResult(1, "", self.stderr, 5)


def _analyzer(tmp_path: Path, runner: FakeCodexRunner) -> CodexAnalyzer:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("仅分析输入 JSON, 不调用任何工具。", encoding="utf-8")
    return CodexAnalyzer(
        AnalysisConfig(max_attempts=2),
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        prompt_path=prompt,
        process_runner=runner,
        clock=lambda: TEST_NOW,
    )


async def test_analyzer_uses_ephemeral_read_only_strict_model_invocation(
    tmp_path: Path,
) -> None:
    runner = FakeCodexRunner([_response(_assessment())])
    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        tracked_symbols=["CYSUSDT"],
        mode=CycleMode.EMERGENCY,
    )

    command = runner.commands[0]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert result.response.assessments[0].strength is SignalStrength.NO_STRONG_SIGNAL
    assert result.usage == {"input_tokens": 100, "output_tokens": 20}
    assert "tracked_symbols_without_prior_direction" in runner.prompts[0]
    assert '"max_target_distance_percent":8.0' in runner.prompts[0]
    assert '"monitoring_valid_for_seconds":{"maximum":3600,"minimum":1800}' in runner.prompts[0]


async def test_analyzer_retries_invalid_evidence_reference(tmp_path: Path) -> None:
    invalid = _response(_assessment(evidence_id="CYSUSDT.UNKNOWN"))
    valid = _response(_assessment())
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 2
    assert "上一次输出未通过程序校验" in runner.prompts[1]
    assert "unknown evidence" in runner.prompts[1]


async def test_analyzer_rejects_invalid_strong_signal_price_geometry(
    tmp_path: Path,
) -> None:
    invalid_long = _response(
        _assessment(
            strength="STRONG",
            direction="LONG_BIAS",
            target=99,
            invalidation=101,
        )
    )
    runner = FakeCodexRunner([invalid_long])

    with pytest.raises(CodexAnalysisError) as captured:
        await _analyzer(tmp_path, runner).analyze(
            analysis_id="analysis_01",
            bundles=[_bundle()],
            mode=CycleMode.EMERGENCY,
        )

    assert captured.value.code == "INVALID_MODEL_OUTPUT"
    assert len(runner.commands) == 2


async def test_analyzer_retries_outlook_window_shift(tmp_path: Path) -> None:
    invalid = _assessment()
    invalid["next_15m"]["window_start"] = "2026-08-18T04:00:00Z"
    invalid["next_15m"]["window_end"] = "2026-08-18T04:15:00Z"
    runner = FakeCodexRunner([_response(invalid), _response(_assessment())])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 2
    assert "next_15m window differs" in runner.prompts[1]


async def test_analyzer_retries_fhe_style_upward_exhaustion_direction(
    tmp_path: Path,
) -> None:
    bundle = _regime_bundle(
        {
            "latest_completed_close_time": "2026-08-17T16:20:00+00:00",
            "latest_close": 0.03088,
            "return_3_percent": -0.19,
            "atr_14": 0.001007,
            "drawdown_from_rolling_high_atr": 3.10,
            "pivot_high_confirmed_at": "2026-08-17T16:20:00+00:00",
            "pivot_high_age_bars": 0,
            "macd_histogram_12_26_9": 0.00076,
            "directional_efficiency_12": 0.51,
            "range_mid_20": 0.02957,
        },
        {
            "latest_close": 0.0321,
            "return_3_percent": 27.23,
            "atr_14_percent": 2.57,
            "range_mid_20": 0.02957,
        },
    )
    second_bundle, second = _second_selected()
    invalid = _response(
        _forming_now(
            _assessment(
                strength="WATCH",
                selection_rank=1,
                direction="LONG_BIAS",
                target=0.032,
                invalidation=0.029,
            )
        ),
        second,
        mode="SCHEDULED",
    )
    valid = _response(
        _forming_now(
            _assessment(
                strength="WATCH",
                selection_rank=1,
                direction="SHORT_BIAS",
                target=0.029,
                invalidation=0.032,
            )
        ),
        second,
        mode="SCHEDULED",
    )
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[bundle, second_bundle],
    )

    assert result.attempts == 2
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "SHORT_BIAS"
    assert "upward exhaustion" in runner.prompts[1]


async def test_emergency_review_applies_same_fhe_direction_guard(tmp_path: Path) -> None:
    bundle = _regime_bundle(
        {
            "latest_close": 0.03088,
            "return_3_percent": -0.19,
            "atr_14": 0.001007,
            "drawdown_from_rolling_high_atr": 3.10,
            "pivot_high_confirmed_at": "2026-08-17T16:20:00+00:00",
            "pivot_high_age_bars": 0,
        },
        {
            "latest_close": 0.0321,
            "return_3_percent": 27.23,
            "atr_14_percent": 2.57,
        },
    )
    invalid = _response(
        _assessment(
            strength="WATCH",
            direction="LONG_BIAS",
            target=0.032,
            invalidation=0.029,
        )
    )
    valid = _response(
        _assessment(
            strength="WATCH",
            direction="SHORT_BIAS",
            target=0.029,
            invalidation=0.032,
        )
    )
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[bundle],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 2
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "SHORT_BIAS"


async def test_analyzer_accepts_tut_style_positive_near_term_momentum(
    tmp_path: Path,
) -> None:
    bundle = _regime_bundle(
        {
            "latest_completed_close_time": "2026-08-17T16:20:00+00:00",
            "latest_close": 100.0,
            "return_3_percent": 8.44,
            "atr_14": 3.0,
            "drawdown_from_rolling_high_atr": 1.1,
            "pivot_high_age_bars": 0,
            "macd_histogram_12_26_9": 0.9,
            "directional_efficiency_12": 0.60,
            "range_mid_20": 95.0,
        },
        {
            "latest_close": 100.0,
            "return_3_percent": 16.08,
            "atr_14_percent": 3.0,
            "range_mid_20": 95.0,
        },
    )
    strong = _assessment(
        strength="STRONG",
        selection_rank=1,
        direction="LONG_BIAS",
        target=103,
        invalidation=97,
    )
    window_start = TEST_NOW.replace(minute=0, second=0, microsecond=0)
    strong["forming_1h"]["window_start"] = window_start.isoformat()
    strong["forming_1h"]["window_end"] = (window_start + timedelta(hours=1)).isoformat()
    second_bundle, second = _second_selected()
    runner = FakeCodexRunner([_response(strong, second, mode="SCHEDULED")])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[bundle, second_bundle],
    )

    assert result.attempts == 1
    assert result.response.assessments[0].strength is SignalStrength.STRONG


async def test_analyzer_retries_fhe_style_confirmed_near_term_downswing(
    tmp_path: Path,
) -> None:
    bundle = _regime_bundle(
        {
            "latest_close": 0.02936,
            "return_3_percent": -0.24,
            "atr_14": 0.00092,
            "macd_histogram_12_26_9": -0.00013,
            "directional_efficiency_12": -0.21,
            "range_mid_20": 0.02957,
        },
        {
            "latest_close": 0.02936,
            "return_3_percent": -8.54,
            "atr_14_percent": 3.36,
            "range_mid_20": 0.02957,
        },
    )
    second_bundle, second = _second_selected()
    invalid = _response(
        _forming_now(
            _assessment(
                strength="WATCH",
                selection_rank=1,
                direction="LONG_BIAS",
                target=0.0305,
                invalidation=0.028,
            )
        ),
        second,
        mode="SCHEDULED",
    )
    valid = _response(
        _forming_now(
            _assessment(
                strength="WATCH",
                selection_rank=1,
                direction="SHORT_BIAS",
                target=0.028,
                invalidation=0.0305,
            )
        ),
        second,
        mode="SCHEDULED",
    )
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[bundle, second_bundle],
    )

    assert result.attempts == 2
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "SHORT_BIAS"
    assert "confirmed near-term downswing" in runner.prompts[1]


@pytest.mark.parametrize(
    ("metric", "valid_for_seconds", "expected_error"),
    [
        ("LAST_PRICE", 60, "between 1800 and 3600 seconds"),
        ("CVD_5M", 3600, "unsupported realtime metric"),
    ],
)
async def test_analyzer_retries_invalid_monitoring_directive(
    tmp_path: Path,
    metric: str,
    valid_for_seconds: int,
    expected_error: str,
) -> None:
    invalid_assessment = _assessment()
    invalid_assessment["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.5m",
            "metric": metric,
            "comparator": "GREATER_THAN",
            "threshold": 101,
            "hysteresis": 0.1,
            "valid_for_seconds": valid_for_seconds,
            "reason": "结构突破后重新分析",
            "evidence_ids": ["CYSUSDT.PA.5M"],
        }
    ]
    runner = FakeCodexRunner([_response(invalid_assessment), _response(_assessment())])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 2
    assert expected_error in runner.prompts[1]


async def test_analyzer_does_not_retry_permanent_auth_failure(tmp_path: Path) -> None:
    runner = FailingCodexRunner("401 Unauthorized: missing bearer or basic authentication")

    with pytest.raises(CodexAnalysisError) as captured:
        await _analyzer(tmp_path, runner).analyze(
            analysis_id="analysis_01",
            bundles=[_bundle()],
            mode=CycleMode.EMERGENCY,
        )

    assert captured.value.code == "CODEX_AUTH_REQUIRED"
    assert runner.calls == 1
