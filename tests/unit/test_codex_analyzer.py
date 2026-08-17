from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bybit_signal.analysis.codex import (
    CodexAnalysisError,
    CodexAnalyzer,
    CodexProcessResult,
)
from bybit_signal.config import AnalysisConfig
from bybit_signal.domain.enums import PriceType, SignalStrength, ToolStatus
from bybit_signal.domain.models import (
    CanonicalPrice,
    EvidenceBundle,
    EvidenceItem,
    ToolAssessment,
)


def _bundle() -> EvidenceBundle:
    now = datetime.now(UTC)
    evidence = EvidenceItem(
        evidence_id="CYSUSDT.PA.5M",
        category="price_action",
        source="TEST",
        observed_at=now,
        summary="completed candles",
        values={"latest_close": 100.0},
    )
    return EvidenceBundle(
        symbol="CYSUSDT",
        generated_at=now,
        source_snapshot_sha256="a" * 64,
        canonical_last=CanonicalPrice(
            symbol="CYSUSDT",
            price_type=PriceType.LAST,
            value=Decimal("100"),
            timestamp=now,
        ),
        canonical_mark=CanonicalPrice(
            symbol="CYSUSDT",
            price_type=PriceType.MARK,
            value=Decimal("100.1"),
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


def _assessment(
    *,
    evidence_id: str = "CYSUSDT.PA.5M",
    strength: str = "NO_STRONG_SIGNAL",
    direction: str | None = None,
    target: float | None = None,
    invalidation: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "symbol": "CYSUSDT",
        "strength": strength,
        "direction": direction,
        "market_state": "测试市场状态",
        "take_profit": None,
        "forming_1h": None,
        "invalidation": None,
        "summary": "当前没有足够强的交易机会",
        "details": "已检查输入证据, 当前不满足强信号条件。",
        "uncertainties": [],
        "evidence_ids": [evidence_id],
        "monitoring_directives": [],
    }
    if strength == "STRONG":
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
    return result


def _response(assessment: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis_id": "analysis_01",
        "cycle_summary": "本轮完成一个币种的独立判断。",
        "assessments": [assessment],
    }


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


def _analyzer(tmp_path: Path, runner: FakeCodexRunner) -> CodexAnalyzer:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("仅分析输入 JSON, 不调用任何工具。", encoding="utf-8")
    return CodexAnalyzer(
        AnalysisConfig(max_attempts=2),
        workspace=tmp_path,
        runtime_root=tmp_path / "runtime",
        prompt_path=prompt,
        process_runner=runner,
    )


async def test_analyzer_uses_ephemeral_read_only_strict_model_invocation(
    tmp_path: Path,
) -> None:
    runner = FakeCodexRunner([_response(_assessment())])
    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
        tracked_symbols=["CYSUSDT"],
    )

    command = runner.commands[0]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert result.response.assessments[0].strength is SignalStrength.NO_STRONG_SIGNAL
    assert result.usage == {"input_tokens": 100, "output_tokens": 20}
    assert "tracked_symbols_without_prior_direction" in runner.prompts[0]


async def test_analyzer_retries_invalid_evidence_reference(tmp_path: Path) -> None:
    invalid = _response(_assessment(evidence_id="CYSUSDT.UNKNOWN"))
    valid = _response(_assessment())
    runner = FakeCodexRunner([invalid, valid])

    result = await _analyzer(tmp_path, runner).analyze(
        analysis_id="analysis_01",
        bundles=[_bundle()],
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
        )

    assert captured.value.code == "INVALID_MODEL_OUTPUT"
    assert len(runner.commands) == 2
