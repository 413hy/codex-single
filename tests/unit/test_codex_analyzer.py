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
    CodexProcessRunner,
    _candle_outlook_windows,
    _model_bundle_payload,
    revalidate_monitoring_assessment,
)
from bybit_signal.config import AnalysisConfig, MonitoringConfig
from bybit_signal.domain.enums import (
    CycleMode,
    MonitoringReviewDecision,
    PriceType,
    SignalStrength,
    ToolStatus,
)
from bybit_signal.domain.models import (
    AnalysisToolResult,
    BreadthSnapshot,
    CandidateAssessment,
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    MarketContext,
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
    one_minute = EvidenceItem(
        evidence_id=f"{symbol}.PA.1M",
        category="price_action",
        source="TEST",
        observed_at=now,
        summary="completed one-minute candles",
        values={"latest_close": float(price), "atr_14": 1.0, "latest_turnover": 10_000.0},
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
        evidence_items=(one_minute, evidence),
        tool_assessments=(
            ToolAssessment(
                tool="TEST_EVIDENCE",
                status=ToolStatus.AVAILABLE,
                reason="fixture evidence is available",
                evidence_ids=(evidence.evidence_id,),
            ),
        ),
    )


def test_model_payload_references_single_batch_market_context() -> None:
    bundle = _bundle()
    now = bundle.generated_at
    context = MarketContext(
        generated_at=now,
        breadth=BreadthSnapshot(
            observed_at=now,
            instrument_count=2,
            advancing_count=1,
            declining_count=1,
            unchanged_count=0,
        ),
    )
    context_item = EvidenceItem(
        evidence_id="CYSUSDT.MARKET.CONTEXT",
        category="market_context",
        source="TEST",
        observed_at=now,
        summary="duplicated batch context",
        values=context.model_dump(mode="json"),
    )
    payload = _model_bundle_payload(
        bundle.model_copy(
            update={
                "market_context": context,
                "evidence_items": (*bundle.evidence_items, context_item),
            }
        ),
        MonitoringConfig(),
    )

    assert "market_context" not in payload
    assert payload["evidence_items"][-1]["values"] == {
        "batch_market_context_ref": "batch_market_context"
    }
    assert "host_target_preflight" not in payload
    assert payload["monitoring_observable_baselines"] == {
        "COMPLETED_1M_CLOSE": "100.0",
        "COMPLETED_5M_CLOSE": "100.0",
        "LAST_PRICE": "100",
        "MARK_PRICE": "100.1",
        "TURNOVER_1M": "10000.0",
    }


def _regime_bundle(
    five_values: dict[str, Any],
    fifteen_values: dict[str, Any],
) -> EvidenceBundle:
    bundle = _bundle()
    now = bundle.generated_at
    latest = Decimal(str(five_values["latest_close"]))
    one_minute_atr = abs(latest) * Decimal("0.01")
    items = (
        EvidenceItem(
            evidence_id="CYSUSDT.PA.1M",
            category="price_action",
            source="TEST",
            observed_at=now,
            summary="completed 1m regime evidence",
            values={
                "latest_close": float(latest),
                "atr_14": float(one_minute_atr),
                "latest_turnover": 1_000_000.0,
            },
        ),
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
    return bundle.model_copy(
        update={
            "canonical_last": bundle.canonical_last.model_copy(update={"value": latest}),
            "canonical_mark": bundle.canonical_mark.model_copy(
                update={"value": latest + Decimal("0.00001")}
            ),
            "evidence_items": items,
        }
    )


def test_model_payload_exposes_raw_evidence_without_host_direction_judgment() -> None:
    bundle = _regime_bundle(
        {
            "latest_close": 100,
            "atr_14": 2,
            "confirmed_pivot_high": 104,
            "rolling_high_20": 105,
            "confirmed_pivot_low": 96,
            "rolling_low_20": 95,
            "return_3_percent": 2,
            "macd_histogram_12_26_9": 0.5,
            "directional_efficiency_12": 0.4,
        },
        {
            "latest_close": 100,
            "confirmed_pivot_high": 103,
            "rolling_high_20": 106,
            "confirmed_pivot_low": 97,
            "rolling_low_20": 94,
            "return_3_percent": 5,
            "atr_14_percent": 2,
            "range_mid_20": 95,
        },
    )

    payload = _model_bundle_payload(bundle, MonitoringConfig())

    assert "host_target_preflight" not in payload
    assert "host_direction_preflight" not in payload
    assert "host_monitoring_preflight" not in payload
    assert payload["monitoring_observable_baselines"]["COMPLETED_5M_CLOSE"] == "100.0"
    fifteen = next(
        item for item in payload["evidence_items"] if item["evidence_id"].endswith(".PA.15M")
    )
    assert fifteen["values"]["return_3_percent"] == 5


def test_activation_keeps_model_thresholds_when_reviewed_baseline_is_unchanged() -> None:
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.too_close",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.6,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "inside the configured half-ATR noise floor",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        },
        {
            "family_id": "cysusdt.structure.valid",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.4,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "outside noise but still near the current structure",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        },
        {
            "family_id": "cysusdt.structure.too_far",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 97.1,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "outside the ordinary two-ATR maximum",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        },
    ]
    result = revalidate_monitoring_assessment(
        CandidateAssessment.model_validate(document),
        _bundle(),
        MonitoringConfig(),
    )

    assert [
        directive.family_id for directive in result.assessment.monitoring_directives
    ] == [
        "cysusdt.structure.too_close",
        "cysusdt.structure.valid",
        "cysusdt.structure.too_far",
    ]
    assert result.dropped_directives == ()


def test_completed_candle_threshold_uses_its_own_baseline_not_last_price() -> None:
    bundle = _bundle().model_copy(
        update={
            "canonical_last": _bundle().canonical_last.model_copy(
                update={"value": Decimal("99")}
            )
        }
    )
    document = _assessment(
        visible=True,
        direction="SHORT_BIAS",
        target=97,
        invalidation=103,
    )
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.completed5.too_close",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "GREATER_THAN",
            "threshold": 100.1,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "one tick above the completed 5m close despite a distant last price",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        }
    ]

    result = revalidate_monitoring_assessment(
        CandidateAssessment.model_validate(document),
        bundle,
        MonitoringConfig(),
    )

    directive = result.assessment.monitoring_directives[0]
    assert directive.current_value == Decimal("100.0")
    assert directive.threshold == Decimal("100.1")
    assert result.dropped_directives == ()


def test_activation_rejects_primary_condition_crossed_before_activation() -> None:
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.reviewed",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.0,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "reviewed against the previous completed 5m baseline",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        }
    ]
    changed_items = tuple(
        item.model_copy(update={"values": {**item.values, "latest_close": 98.0}})
        if item.evidence_id.endswith(".PA.5M")
        else item
        for item in _bundle().evidence_items
    )
    changed = _bundle().model_copy(update={"evidence_items": changed_items})

    with pytest.raises(ValueError, match="complete primary condition is already met"):
        revalidate_monitoring_assessment(
            CandidateAssessment.model_validate(document),
            changed,
            MonitoringConfig(),
        )


def test_activation_keeps_multi_observation_rule_after_only_latest_crossing() -> None:
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.two-closes",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.0,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "structured field requires more than one completed observation",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
            "required_consecutive_observations": 2,
        }
    ]
    changed_items = tuple(
        item.model_copy(update={"values": {**item.values, "latest_close": 98.0}})
        if item.evidence_id.endswith(".PA.5M")
        else item
        for item in _bundle().evidence_items
    )
    changed = _bundle().model_copy(update={"evidence_items": changed_items})

    result = revalidate_monitoring_assessment(
        CandidateAssessment.model_validate(document),
        changed,
        MonitoringConfig(),
    )

    directive = result.assessment.monitoring_directives[0]
    assert directive.current_value == Decimal("98.0")
    assert directive.required_consecutive_observations == 2


def test_activation_rebases_primary_completed_candle_moved_toward_but_unmet() -> None:
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.reviewed",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.0,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "reviewed fixed structural threshold",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        }
    ]
    changed_items = tuple(
        item.model_copy(update={"values": {**item.values, "latest_close": 99.5}})
        if item.evidence_id.endswith(".PA.5M")
        else item
        for item in _bundle().evidence_items
    )
    changed = _bundle().model_copy(update={"evidence_items": changed_items})

    result = revalidate_monitoring_assessment(
        CandidateAssessment.model_validate(document),
        changed,
        MonitoringConfig(),
    )

    assert result.assessment.monitoring_directives[0].current_value == Decimal("99.5")


def test_activation_rebases_primary_completed_candle_moved_away_from_threshold() -> None:
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.structure.reviewed",
            "metric": "COMPLETED_5M_CLOSE",
            "comparator": "LESS_THAN",
            "threshold": 99.0,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "reviewed against the previous completed 5m baseline",
            "evidence_ids": ["CYSUSDT.PA.5M"],
            "current_value": 100.0,
        }
    ]
    changed_items = tuple(
        item.model_copy(update={"values": {**item.values, "latest_close": 101.0}})
        if item.evidence_id.endswith(".PA.5M")
        else item
        for item in _bundle().evidence_items
    )
    changed = _bundle().model_copy(update={"evidence_items": changed_items})

    result = revalidate_monitoring_assessment(
        CandidateAssessment.model_validate(document),
        changed,
        MonitoringConfig(),
    )

    assert result.assessment.monitoring_directives[0].current_value == Decimal("101.0")


def test_trade_delta_zero_axis_flip_is_not_an_actionable_threshold() -> None:
    bundle = _bundle()
    trade_evidence = EvidenceItem(
        evidence_id="CYSUSDT.MICRO.TRADES.30S",
        category="order_flow",
        source="TEST",
        observed_at=bundle.generated_at,
        summary="qualified 30-second trade window",
        values={
            "qualified": True,
            "buy_notional": 10_000.0,
            "sell_notional": 5_000.0,
            "signed_delta_notional": 5_000.0,
        },
    )
    bundle = bundle.model_copy(
        update={"evidence_items": (*bundle.evidence_items, trade_evidence)}
    )
    document = _assessment(visible=True)
    document["monitoring_directives"] = [
        {
            "family_id": "cysusdt.trade.zero_axis",
            "metric": "TRADE_DELTA_30S",
            "comparator": "LESS_THAN",
            "threshold": 0,
            "hysteresis": 1_000,
            "valid_for_seconds": 3600,
            "reason": "a light rolling-window sign flip must not wake Codex",
            "evidence_ids": ["CYSUSDT.MICRO.TRADES.30S"],
        }
    ]

    with pytest.raises(ValueError, match="completed-candle price structure"):
        revalidate_monitoring_assessment(
            CandidateAssessment.model_validate(document),
            bundle,
            MonitoringConfig(),
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
    if selection_rank is not None or visible:
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
                "severity": "CRITICAL",
                "confirmation": "completed_5m",
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
        self.cwds: list[Path] = []

    async def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        stdin: str,
    ) -> CodexProcessResult:
        del timeout_seconds
        command_tuple = tuple(command)
        self.commands.append(command_tuple)
        self.prompts.append(stdin)
        self.cwds.append(cwd)
        output = Path(command_tuple[command_tuple.index("--output-last-message") + 1])
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


class FakeToolRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def execute_many(
        self,
        requests: Sequence[Any],
        *,
        allowed_symbols: frozenset[str],
    ) -> tuple[AnalysisToolResult, ...]:
        self.calls.append(tuple(request.request_id for request in requests))
        assert allowed_symbols == frozenset({"CYSUSDT"})
        now = datetime.now(UTC)
        return tuple(
            AnalysisToolResult(
                request_id=request.request_id,
                tool=request.tool,
                symbol=request.symbol,
                status=ToolStatus.AVAILABLE,
                requested_at=now,
                completed_at=now,
                latency_ms=1,
                evidence_items=(
                    EvidenceItem(
                        evidence_id="CYSUSDT.TOOL.LATEST_MARKET.FRESH_01",
                        category="tool_latest_market",
                        source="TEST_HOST_TOOL",
                        observed_at=now,
                        summary="fresh test market evidence",
                        values={"last_price": 100.0},
                    ),
                ),
            )
            for request in requests
        )


def _analyzer(tmp_path: Path, runner: CodexProcessRunner) -> CodexAnalyzer:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("仅分析输入 JSON, 不调用任何工具。", encoding="utf-8")
    (tmp_path / "monitoring_review_zh.md").write_text(
        "只复核反向完成柱结构规则。", encoding="utf-8"
    )
    return CodexAnalyzer(
        AnalysisConfig(max_attempts=2),
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        prompt_path=prompt,
        process_runner=runner,
        clock=lambda: TEST_NOW,
    )


def _monitoring_review_item(
    symbol: str,
    decision: str,
    *,
    rationale: str,
    family_suffix: str = "counter-direction.5m",
) -> dict[str, Any]:
    directives: list[dict[str, Any]] = []
    if decision != "REJECTED":
        directives.append(
            {
                "family_id": f"{symbol.lower()}.{family_suffix}",
                "metric": "COMPLETED_5M_CLOSE",
                "comparator": "LESS_THAN",
                "threshold": 98,
                "hysteresis": 0.1,
                "valid_for_seconds": 3600,
                "reason": "完成5m跌破已确认反向枢轴后重新分析",
                "evidence_ids": [f"{symbol}.PA.5M"],
                "current_value": 100,
                "confirmation": "一根完成5m收盘低于阈值",
            }
        )
    return {
        "symbol": symbol,
        "decision": decision,
        "rationale": rationale,
        "directives": directives,
    }


def _monitoring_review_response(*reviews: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": list(reviews),
    }


async def test_analyzer_uses_ephemeral_read_only_strict_model_invocation(
    tmp_path: Path,
) -> None:
    runner = FakeCodexRunner([_response(_assessment())])
    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        tracked_symbols=["CYSUSDT"],
        trigger_reasons=["TRADE_DELTA_30S=-4448 crossed -3700: 卖方成交显著扩大"],
        mode=CycleMode.EMERGENCY,
    )

    command = runner.commands[0]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-terra"
    disabled = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--disable"
    }
    assert disabled == {
        "apps",
        "browser_use",
        "computer_use",
        "plugins",
        "shell_tool",
        "skill_search",
    }
    command_cwd = Path(command[command.index("--cd") + 1])
    assert runner.cwds == [command_cwd]
    assert command_cwd != tmp_path
    assert result.response.assessments[0].strength is SignalStrength.NO_STRONG_SIGNAL
    assert result.usage == {"input_tokens": 100, "output_tokens": 20}
    assert "tracked_symbols_without_prior_direction" in runner.prompts[0]
    trigger_context = (
        '"emergency_trigger_reasons":'
        '["TRADE_DELTA_30S=-4448 crossed -3700: 卖方成交显著扩大"]'
    )
    assert trigger_context in runner.prompts[0]
    assert "max_target_distance_percent" not in runner.prompts[0]
    assert '"monitoring_valid_for_seconds":{"maximum":3600,"minimum":1800}' in runner.prompts[0]


async def test_analyzer_does_not_gate_direction_on_approximate_take_profit(
    tmp_path: Path,
) -> None:
    bundle = _regime_bundle(
        {
            "latest_close": 100,
            "atr_14": 2,
            "confirmed_pivot_high": 104,
            "rolling_high_20": 105,
            "confirmed_pivot_low": 96,
            "rolling_low_20": 95,
        },
        {
            "latest_close": 100,
            "confirmed_pivot_high": 103,
            "rolling_high_20": 106,
            "confirmed_pivot_low": 97,
            "rolling_low_20": 94,
        },
    )
    invalid = _assessment(target=101, invalidation=97)
    runner = FakeCodexRunner([_response(invalid)])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[bundle],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 1
    assert result.response.assessments[0].take_profit is not None
    assert result.response.assessments[0].take_profit.value == Decimal("101")


async def test_analyzer_executes_bounded_host_tool_then_validates_final(
    tmp_path: Path,
) -> None:
    tool_turn = {
        "schema_version": 1,
        "action": "TOOL_REQUESTS",
        "requests": [
            {
                "request_id": "fresh_01",
                "tool": "latest_market",
                "symbol": "CYSUSDT",
                "arguments": {"timeframes": None, "limit": None},
                "reason": "确认模型运行期间的最新价格",
            }
        ],
        "final": None,
    }
    final_turn = {
        "schema_version": 1,
        "action": "FINAL",
        "requests": [],
        "final": _response(_assessment()),
    }
    runner = FakeCodexRunner([tool_turn, final_turn])
    registry = FakeToolRegistry()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("只使用输入证据与宿主管理工具。", encoding="utf-8")
    analyzer = CodexAnalyzer(
        AnalysisConfig(max_attempts=2, max_tool_rounds=1),
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        prompt_path=prompt,
        process_runner=runner,
        clock=lambda: TEST_NOW,
        tool_registry=registry,  # type: ignore[arg-type]
    )

    result = await analyzer.analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert registry.calls == [("fresh_01",)]
    assert len(runner.commands) == 2
    assert "completed_tool_results" in runner.prompts[1]
    assert '"signal_history"' not in runner.prompts[0]
    assert "CYSUSDT.TOOL.LATEST_MARKET.FRESH_01" in runner.prompts[1]
    assert len(result.tool_results) == 1
    assert result.evidence_bundles[0].evidence_items[-1].category == "tool_latest_market"


async def test_host_replaces_model_supplied_monitoring_current_value(
    tmp_path: Path,
) -> None:
    assessment = _assessment()
    assessment["monitoring_directives"][0]["current_value"] = 1
    assessment["monitoring_directives"][0]["distance_percent"] = 99
    runner = FakeCodexRunner([_response(assessment)])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    directive = result.response.assessments[0].monitoring_directives[0]
    assert directive.current_value == Decimal("100")
    assert directive.distance_percent == Decimal("3")


async def test_main_analysis_preserves_candidate_thresholds_for_model_review(
    tmp_path: Path,
) -> None:
    assessment = _assessment()
    assessment["monitoring_directives"].append(
        {
            "family_id": "cysusdt.noisy.last",
            "metric": "LAST_PRICE",
            "comparator": "GREATER_THAN",
            "threshold": 100.001,
            "hysteresis": 0.001,
            "valid_for_seconds": 3600,
            "reason": "这个阈值位于正常噪声内, 应由宿主剔除",
            "evidence_ids": ["CYSUSDT.PA.5M"],
        }
    )
    runner = FakeCodexRunner([_response(assessment)])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    directives = result.response.assessments[0].monitoring_directives
    assert [directive.family_id for directive in directives] == [
        "cysusdt.invalidation.5m",
        "cysusdt.noisy.last",
    ]
    assert result.attempts == 1
    assert "host_dropped_monitoring_directives" not in result.attempt_diagnostics[0]


async def test_main_analysis_does_not_erase_target_candidate_before_model_review(
    tmp_path: Path,
) -> None:
    assessment = _assessment()
    assessment["monitoring_directives"].append(
        {
            "family_id": "cysusdt.target.last",
            "metric": "LAST_PRICE",
            "comparator": "GREATER_THAN",
            "threshold": 103,
            "hysteresis": 0.1,
            "valid_for_seconds": 3600,
            "reason": "目标价到达后重新分析",
            "evidence_ids": ["CYSUSDT.PA.5M"],
        }
    )
    runner = FakeCodexRunner([_response(assessment)])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    directives = result.response.assessments[0].monitoring_directives
    assert [directive.family_id for directive in directives] == [
        "cysusdt.invalidation.5m",
        "cysusdt.target.last",
    ]


async def test_monitoring_review_mechanically_rejects_target_rule(
    tmp_path: Path,
) -> None:
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "ACCEPTED",
                "rationale": "错误地把展示目标作为规则",
                "directives": [
                    {
                        "family_id": "cysusdt.target.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "GREATER_THAN",
                        "threshold": 103,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "止盈目标到达后重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m",
                    }
                ],
            }
        ],
    }
    analyzer = _analyzer(tmp_path, FakeCodexRunner([review]))

    with pytest.raises(CodexAnalysisError) as captured:
        await analyzer.review_monitoring(
            analysis_id="analysis_01",
            assessments=[CandidateAssessment.model_validate(_assessment())],
            bundles=[_bundle()],
        )

    assert captured.value.code == "MONITORING_REVIEW_NOT_EXECUTABLE"
    assert "take-profit or target" in str(captured.value)


async def test_monitoring_review_accepts_price_primary_with_trade_confirmation(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    trade = EvidenceItem(
        evidence_id="CYSUSDT.MICRO.TRADES.30S",
        category="order_flow",
        source="TEST",
        observed_at=bundle.generated_at,
        summary="qualified trade window",
        values={
            "qualified": True,
            "buy_notional": 10_000.0,
            "sell_notional": 5_000.0,
            "signed_delta_notional": 5_000.0,
        },
    )
    bundle = bundle.model_copy(
        update={"evidence_items": (*bundle.evidence_items, trade)}
    )
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "REWRITTEN",
                "rationale": "完成5m反向破位并由卖方成交确认才值得复核",
                "directives": [
                    {
                        "family_id": "cysusdt.threat.5m_trade",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "LESS_THAN",
                        "threshold": 98,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "反向完成5m结构配合成交差转弱",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m_and_rolling_30s",
                        "confirmation_seconds": 3,
                        "confirmations": [
                            {
                                "metric": "TRADE_DELTA_30S",
                                "comparator": "LESS_THAN",
                                "threshold": -1000,
                                "current_value": 5000,
                                "evidence_ids": ["CYSUSDT.MICRO.TRADES.30S"],
                                "confirmation": "rolling_30s_complete",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = await _analyzer(tmp_path, FakeCodexRunner([review])).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[bundle],
    )

    directive = result.response.reviews[0].directives[0]
    assert directive.metric.value == "COMPLETED_5M_CLOSE"
    assert directive.confirmations[0].metric.value == "TRADE_DELTA_30S"


async def test_monitoring_review_treats_no_qualified_rule_as_valid_without_retry(
    tmp_path: Path,
) -> None:
    initial_reason = "候选阈值只对应普通1m噪声, 缺少完成5m结构锚点"
    runner = FakeCodexRunner(
        [
            _monitoring_review_response(
                _monitoring_review_item(
                    "CYSUSDT",
                    "REJECTED",
                    rationale=initial_reason,
                )
            )
        ]
    )

    result = await _analyzer(tmp_path, runner).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
    )

    assert result.attempts == 1
    assert len(runner.prompts) == 1
    assert result.response.reviews[0].decision is MonitoringReviewDecision.REJECTED
    repair = result.repair_diagnostics[0]
    assert repair["initial_decision"] == "REJECTED"
    assert repair["repair_attempts"] == 0
    assert repair["final_decision"] == "REJECTED"
    assert repair["exhausted"] is False
    context = json.loads(runner.prompts[0].split("# 本轮输入\n", 1)[1])
    assert context["policy"]["rules_per_symbol"] == {"minimum": 0, "maximum": 3}


async def test_monitoring_review_receives_activation_error_with_fresh_baseline(
    tmp_path: Path,
) -> None:
    activation_error = (
        "reviewed completed-candle baseline moved toward the threshold before activation"
    )
    runner = FakeCodexRunner(
        [
            _monitoring_review_response(
                _monitoring_review_item(
                    "CYSUSDT",
                    "REWRITTEN",
                    rationale="已按交付时新基线重新审计结构距离",
                )
            )
        ]
    )

    result = await _analyzer(tmp_path, runner).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
        repair_reasons={"CYSUSDT": (activation_error,)},
        maximum_repair_attempts=0,
    )

    assert result.attempts == 1
    context = json.loads(runner.prompts[0].split("# 本轮输入\n", 1)[1])
    assert context["repair_context"]["symbols"][0]["previous_rejections"] == [
        activation_error
    ]


async def test_monitoring_review_keeps_accepted_rule_and_valid_rejection_in_one_call(
    tmp_path: Path,
) -> None:
    gps_bundle, gps_assessment_document = _second_selected()
    cys_assessment = CandidateAssessment.model_validate(_assessment())
    gps_assessment = CandidateAssessment.model_validate(gps_assessment_document)
    accepted_cys = _monitoring_review_item(
        "CYSUSDT",
        "ACCEPTED",
        rationale="CYS规则已经使用已确认完成5m反向枢轴",
        family_suffix="frozen.5m",
    )
    runner = FakeCodexRunner(
        [
            _monitoring_review_response(
                accepted_cys,
                _monitoring_review_item(
                    "GPSUSDT",
                    "REJECTED",
                    rationale="GPS候选缺少真实中间结构锚点",
                ),
            )
        ]
    )

    result = await _analyzer(tmp_path, runner).review_monitoring(
        analysis_id="analysis_01",
        assessments=[cys_assessment, gps_assessment],
        bundles=[_bundle(), gps_bundle],
    )

    assert [review.symbol for review in result.response.reviews] == [
        "CYSUSDT",
        "GPSUSDT",
    ]
    accepted = result.response.reviews[0]
    assert accepted.decision is MonitoringReviewDecision.ACCEPTED
    assert accepted.directives[0].family_id == "cysusdt.frozen.5m"
    assert accepted.directives[0].current_value == Decimal("100")
    assert result.response.reviews[1].decision is MonitoringReviewDecision.REJECTED
    assert result.attempts == 1
    assert result.repair_diagnostics[0]["repair_attempts"] == 0
    assert result.repair_diagnostics[1]["repair_attempts"] == 0
    assert result.repair_diagnostics[1]["exhausted"] is False


async def test_monitoring_review_explicit_repair_budget_does_not_force_semantic_repair(
    tmp_path: Path,
) -> None:
    rejected = _monitoring_review_response(
        _monitoring_review_item(
            "CYSUSDT",
            "REJECTED",
            rationale="没有位于当前完成柱和正式失效之间的可靠结构锚点",
        )
    )
    runner = FakeCodexRunner([rejected])

    result = await _analyzer(tmp_path, runner).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
        maximum_repair_attempts=3,
    )

    assert result.attempts == 1
    assert len(runner.prompts) == 1
    assert result.response.reviews[0].decision is MonitoringReviewDecision.REJECTED
    repair = result.repair_diagnostics[0]
    assert repair["repair_attempts"] == 0
    assert repair["final_decision"] == "REJECTED"
    assert repair["exhausted"] is False
    assert len(repair["repair_history"]) == 1


async def test_monitoring_review_retries_contract_error_and_omits_take_profit(
    tmp_path: Path,
) -> None:
    invalid = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "ACCEPTED",
                "rationale": "错误地监测多头同方向上涨",
                "directives": [
                    {
                        "family_id": "cysusdt.same-direction.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "GREATER_THAN",
                        "threshold": 101,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "完成5m继续上涨后重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m",
                    }
                ],
            }
        ],
    }
    valid = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "REWRITTEN",
                "rationale": "已改为正式失效前的反向5m价格结构威胁",
                "directives": [
                    {
                        "family_id": "cysusdt.counter-direction.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "LESS_THAN",
                        "threshold": 98,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "完成5m跌破近端枢轴后重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m",
                    }
                ],
            }
        ],
    }
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
    )

    first_context = json.loads(runner.prompts[0].split("# 本轮输入\n", 1)[1])
    assert "take_profit" not in first_context["signals"][0]
    assert result.attempts == 2
    assert [item["status"] for item in result.attempt_diagnostics] == [
        "REVIEW_REJECTED",
        "REVIEW_ACCEPTED",
    ]
    assert "上一次输出未通过程序校验" in runner.prompts[1]


async def test_monitoring_review_accepts_rule_at_formal_invalidation_for_reassessment(
    tmp_path: Path,
) -> None:
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "ACCEPTED",
                "rationale": "错误地直接复用正式方向失效位",
                "directives": [
                    {
                        "family_id": "cysusdt.formal-invalidation.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "LESS_THAN",
                        "threshold": 97,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "完成5m触及正式失效位后重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m",
                    }
                ],
            }
        ],
    }

    result = await _analyzer(tmp_path, FakeCodexRunner([review])).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
    )

    assert result.attempts == 1
    assert result.response.reviews[0].decision is MonitoringReviewDecision.ACCEPTED


async def test_monitoring_review_rejects_rule_beyond_formal_invalidation(
    tmp_path: Path,
) -> None:
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "ACCEPTED",
                "rationale": "错误地把唤醒线放到了正式方向失效位之外",
                "directives": [
                    {
                        "family_id": "cysusdt.beyond-formal-invalidation.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "LESS_THAN",
                        "threshold": 96,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "完成5m越过正式失效位很远以后才重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "completed_5m",
                    }
                ],
            }
        ],
    }

    with pytest.raises(CodexAnalysisError) as captured:
        await _analyzer(tmp_path, FakeCodexRunner([review])).review_monitoring(
            analysis_id="analysis_01",
            assessments=[CandidateAssessment.model_validate(_assessment())],
            bundles=[_bundle()],
        )

    assert captured.value.code == "MONITORING_REVIEW_NOT_EXECUTABLE"
    assert "beyond the formal LONG direction invalidation" in str(captured.value)


async def test_hemi_short_uses_fast_close_at_formal_structure_price_in_one_pass(
    tmp_path: Path,
) -> None:
    bundle = _bundle("HEMIUSDT", "0.008492")
    assessment_document = _assessment(
        symbol="HEMIUSDT",
        evidence_id="HEMIUSDT.PA.5M",
        direction="SHORT_BIAS",
        target=0.008055,
        invalidation=0.008583,
    )
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "HEMIUSDT",
                "decision": "REWRITTEN",
                "rationale": (
                    "没有独立中间锚点, 改用完成1m观察正式5m结构价; "
                    "价格相同但执行周期更快, crossing只复核。"
                ),
                "directives": [
                    {
                        "family_id": "hemi.short.formal-structure.fast-close",
                        "metric": "COMPLETED_1M_CLOSE",
                        "comparator": "GREATER_THAN",
                        "threshold": 0.008583,
                        "hysteresis": 0.000028,
                        "valid_for_seconds": 1800,
                        "reason": "完成1m重新站上正式5m结构价时唤醒方向复核",
                        "evidence_ids": ["HEMIUSDT.PA.1M", "HEMIUSDT.PA.5M"],
                        "current_value": 0.008492,
                        "confirmation": "一根完成1m收盘站上结构价",
                        "required_consecutive_observations": 1,
                    }
                ],
            }
        ],
    }

    result = await _analyzer(tmp_path, FakeCodexRunner([review])).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(assessment_document)],
        bundles=[bundle],
        maximum_repair_attempts=0,
    )

    directive = result.response.reviews[0].directives[0]
    assert result.attempts == 1
    assert directive.metric.value == "COMPLETED_1M_CLOSE"
    assert directive.threshold == Decimal("0.008583")


def test_runtime_prompts_prefer_nearest_anchor_and_allow_no_monitoring() -> None:
    analysis_prompt = Path("prompts/signal_analysis_zh.md").read_text(encoding="utf-8")
    review_prompt = Path("prompts/monitoring_review_zh.md").read_text(encoding="utf-8")

    for prompt in (analysis_prompt, review_prompt):
        assert "距当前最近" in prompt
        assert "invalidation.reference_price" in prompt
        assert "crossing 只" in prompt or "crossing仅" in prompt
        assert "提前" in prompt
    assert "正常降级" in analysis_prompt
    assert "正常结果" in review_prompt
    assert "同方向进入 Top2 还必须有完成柱重置证据" in analysis_prompt


async def test_monitoring_review_does_not_retry_permanent_credit_failure(
    tmp_path: Path,
) -> None:
    runner = FailingCodexRunner("Your workspace is out of credits")

    with pytest.raises(CodexAnalysisError) as captured:
        await _analyzer(tmp_path, runner).review_monitoring(
            analysis_id="analysis_01",
            assessments=[CandidateAssessment.model_validate(_assessment())],
            bundles=[_bundle()],
        )

    assert captured.value.code == "CODEX_CREDITS_EXHAUSTED"
    assert runner.calls == 1


async def test_monitoring_review_uses_structured_execution_count_over_prose(
    tmp_path: Path,
) -> None:
    review = {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "reviews": [
            {
                "symbol": "CYSUSDT",
                "decision": "ACCEPTED",
                "rationale": "需要连续两根完成5m确认反向结构",
                "directives": [
                    {
                        "family_id": "cysusdt.threat.5m",
                        "metric": "COMPLETED_5M_CLOSE",
                        "comparator": "LESS_THAN",
                        "threshold": 98,
                        "hysteresis": 0.1,
                        "valid_for_seconds": 3600,
                        "reason": "连续两根完成5m收盘跌破才重新分析",
                        "evidence_ids": ["CYSUSDT.PA.5M"],
                        "current_value": 100,
                        "confirmation": "two consecutive completed 5m closes",
                        "required_consecutive_observations": 1,
                    }
                ],
            }
        ],
    }

    result = await _analyzer(tmp_path, FakeCodexRunner([review])).review_monitoring(
        analysis_id="analysis_01",
        assessments=[CandidateAssessment.model_validate(_assessment())],
        bundles=[_bundle()],
    )

    assert result.response.reviews[0].directives[
        0
    ].required_consecutive_observations == 1


async def test_emergency_direction_remains_valid_when_candidate_thresholds_are_empty(
    tmp_path: Path,
) -> None:
    missing = _assessment()
    missing["monitoring_directives"] = []
    runner = FakeCodexRunner([_response(missing), _response(_assessment())])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 1
    assert result.response.assessments[0].monitoring_directives == ()
    assert len(runner.commands) == 1


async def test_tool_protocol_caps_rejected_final_retries(tmp_path: Path) -> None:
    invalid = _assessment()
    invalid["evidence_ids"] = ["CYSUSDT.UNKNOWN"]
    invalid_turn = {
        "schema_version": 1,
        "action": "FINAL",
        "requests": [],
        "final": _response(invalid),
    }
    runner = FakeCodexRunner([invalid_turn, invalid_turn, invalid_turn])
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Use only supplied evidence.", encoding="utf-8")
    analyzer = CodexAnalyzer(
        AnalysisConfig(max_attempts=2, max_tool_rounds=3),
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        prompt_path=prompt,
        process_runner=runner,
        clock=lambda: TEST_NOW,
        tool_registry=FakeToolRegistry(),  # type: ignore[arg-type]
    )

    with pytest.raises(CodexAnalysisError) as captured:
        await analyzer.analyze(
            analysis_id="analysis_01",
            bundles=[_bundle()],
            mode=CycleMode.EMERGENCY,
        )

    assert len(runner.commands) == 2
    assert len(captured.value.diagnostics) == 2
    assert all(item["status"] == "FINAL_REJECTED" for item in captured.value.diagnostics)


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


async def test_host_does_not_replace_model_as_strategy_judge_for_price_geometry(
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

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        mode=CycleMode.EMERGENCY,
    )

    assert result.attempts == 1
    assert result.response.assessments[0].direction is not None


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


async def test_fhe_style_direction_is_owned_by_model_not_host_preflight(
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
                invalidation=0.0291,
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

    assert result.attempts == 1
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "LONG_BIAS"
    assert "impact exhaustion/reversal" in runner.prompts[0]


async def test_emergency_direction_is_not_silently_overridden_by_host(tmp_path: Path) -> None:
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

    assert result.attempts == 1
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "LONG_BIAS"


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


async def test_confirmed_near_term_downswing_stays_model_owned(
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

    assert result.attempts == 1
    assert result.response.assessments[0].direction is not None
    assert result.response.assessments[0].direction.value == "LONG_BIAS"


@pytest.mark.parametrize(
    ("metric", "valid_for_seconds", "expected_error"),
    [
        ("LAST_PRICE", 60, "between 1800 and 3600 seconds"),
        ("CVD_5M", 3600, "unsupported realtime metric CVD_5M"),
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
