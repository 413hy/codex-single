from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from bybit_signal.domain.enums import Direction, SignalStrength, TrackingStatus
from bybit_signal.domain.models import AnalysisCycleResult, SignalConclusion

SHANGHAI = ZoneInfo("Asia/Shanghai")


def cycle_notification(cycle: AnalysisCycleResult) -> tuple[str, tuple[str, ...]]:
    generated = cycle.completed_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    important = [
        conclusion
        for conclusion in cycle.conclusions
        if conclusion.assessment.strength is SignalStrength.STRONG
        or conclusion.tracking_status in {TrackingStatus.WEAKENED, TrackingStatus.INVALIDATED}
    ]
    if not important:
        important = list(cycle.conclusions)
    lines = [
        "<b>核心结论</b>",
        f"时间: <b>{escape(generated)} UTC+8</b>",
        f"本轮候选: {escape(', '.join(cycle.candidate_symbols) or '无')}",
        f"强信号: <b>{cycle.strong_signal_count}</b> / {len(cycle.conclusions)}",
        "",
    ]
    for conclusion in important:
        lines.extend(_conclusion_card(conclusion))
        lines.append("")
    if cycle.strong_signal_count == 0:
        lines.append("<i>本轮没有经严格校验的强交易机会, 请勿把候选排名当作方向。</i>")
    text = "\n".join(lines).strip()
    if len(text) > 3900:
        text = text[:3870] + "\n…详情请使用下方按钮查看"
    symbols = tuple(conclusion.assessment.symbol for conclusion in important[:8])
    return text, symbols


def detail_notification(conclusion: SignalConclusion) -> str:
    assessment = conclusion.assessment
    evidence_time = conclusion.generated_at.astimezone(SHANGHAI).isoformat(timespec="seconds")
    lines = [
        f"<b>{escape(assessment.symbol)} 分析详情</b>",
        f"分析 ID: <code>{escape(conclusion.analysis_id)}</code>",
        f"证据时间: {escape(evidence_time)}",
        (
            f"参考价: <b>{conclusion.canonical_price.value} USDT</b> "
            f"({conclusion.canonical_price.price_type.value})"
        ),
        f"强度: <b>{assessment.strength.value}</b>",
        f"方向: <b>{_direction(assessment.direction)}</b>",
        f"市场状态: {escape(assessment.market_state)}",
        f"相比上一轮: {escape(conclusion.comparison_with_previous)}",
        "",
        "<b>综合分析</b>",
        escape(assessment.details),
    ]
    if assessment.take_profit is not None:
        lines.extend(
            (
                "",
                f"唯一目标: <b>{assessment.take_profit.value} USDT</b>",
                f"目标依据: {escape(assessment.take_profit.rationale)}",
            )
        )
    if assessment.forming_1h is not None:
        outlook = assessment.forming_1h
        start = outlook.window_start.astimezone(SHANGHAI).strftime("%H:%M")
        end = outlook.window_end.astimezone(SHANGHAI).strftime("%H:%M")
        lines.extend(
            (
                f"形成中 1h: {_direction(outlook.direction)} / {outlook.strength}",
                f"1h 窗口: {start}-{end} UTC+8",
            )
        )
    if assessment.invalidation is not None:
        lines.extend(
            (
                "",
                f"方向失效参考: <b>{assessment.invalidation.reference_price} USDT</b>",
                escape(assessment.invalidation.condition),
            )
        )
    if assessment.uncertainties:
        lines.extend(("", "<b>不确定性</b>"))
        lines.extend(f"• {escape(value)}" for value in assessment.uncertainties)
    lines.extend(("", "<b>工具状态</b>"))
    lines.extend(
        f"• {escape(tool.tool)}: {tool.status.value} — {escape(tool.reason)}"
        for tool in conclusion.tool_assessments
    )
    lines.extend(
        ("", f"证据 ID: <code>{escape(', '.join(assessment.evidence_ids) or '无')}</code>")
    )
    return "\n".join(lines)


def _conclusion_card(conclusion: SignalConclusion) -> list[str]:
    assessment = conclusion.assessment
    result = [
        (
            f"<b>{escape(assessment.symbol)}</b> | "
            f"{_direction(assessment.direction)} | {assessment.strength.value}"
        ),
        f"参考价: <b>{conclusion.canonical_price.value} USDT</b>",
        f"市场状态: {escape(assessment.market_state)}",
    ]
    if assessment.take_profit is not None:
        result.append(f"大致止盈位: <b>{assessment.take_profit.value} USDT</b>")
    if assessment.forming_1h is not None:
        result.append(
            f"下一根 1h: {_direction(assessment.forming_1h.direction)}; "
            f"{assessment.forming_1h.strength}"
        )
    result.extend(
        (
            f"相比上一轮: {escape(conclusion.comparison_with_previous)}",
            escape(assessment.summary),
        )
    )
    if assessment.invalidation is not None:
        result.append(
            f"方向失效: {escape(assessment.invalidation.condition)} "
            f"({assessment.invalidation.reference_price})"
        )
    return result


def _direction(direction: Direction | None) -> str:
    if direction is Direction.LONG_BIAS:
        return "偏多"
    if direction is Direction.SHORT_BIAS:
        return "偏空"
    return "无明确方向"
