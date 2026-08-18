from __future__ import annotations

from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from bybit_signal.domain.enums import (
    CycleMode,
    CycleStatus,
    Direction,
    TrackingStatus,
)
from bybit_signal.domain.models import AnalysisCycleResult, CandleOutlook, SignalConclusion

SHANGHAI = ZoneInfo("Asia/Shanghai")


def cycle_notification(cycle: AnalysisCycleResult) -> tuple[str, tuple[str, ...]]:
    if cycle.mode is not CycleMode.SCHEDULED or cycle.status is not CycleStatus.SUCCESS:
        raise ValueError("cycle_notification requires a successful scheduled cycle")
    generated = cycle.completed_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    selected = sorted(
        (
            conclusion
            for conclusion in cycle.conclusions
            if conclusion.assessment.selection_rank is not None
        ),
        key=lambda conclusion: conclusion.assessment.selection_rank or 0,
    )
    updates = [
        conclusion
        for conclusion in cycle.conclusions
        if conclusion.assessment.selection_rank is None
        and conclusion.tracking_status
        in {TrackingStatus.EXITED, TrackingStatus.INVALIDATED}
    ]
    lines = [
        "<b>核心结论</b>",
        f"时间: <b>{escape(generated)} UTC+8</b>",
        f"本轮主信号: <b>{cycle.selected_signal_count}</b> / 2",
        "",
    ]
    for conclusion in selected:
        lines.extend(_conclusion_card(conclusion))
        lines.append("")
    if updates:
        lines.append("<b>上轮信号更新</b>")
        for conclusion in updates:
            state = (
                "已失效"
                if conclusion.tracking_status is TrackingStatus.INVALIDATED
                else "已退出本轮"
            )
            lines.append(
                f"• <b>{escape(conclusion.assessment.symbol)}</b>: {state}; "
                f"{escape(conclusion.comparison_with_previous)}"
            )
        lines.append("<i>以上币种已停止实时监测, 不计入本轮两个主信号。</i>")
    text = _bounded_html_lines(
        lines,
        suffix="…详情请使用下方按钮查看",
    )
    symbols = tuple(conclusion.assessment.symbol for conclusion in selected)
    return text, symbols


def emergency_notification(cycle: AnalysisCycleResult) -> tuple[str, tuple[str, ...]]:
    if cycle.mode is not CycleMode.EMERGENCY or cycle.status is not CycleStatus.SUCCESS:
        raise ValueError("emergency_notification requires a successful emergency cycle")
    conclusion = cycle.conclusions[0]
    observed = cycle.completed_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    reasons = cycle.diagnostics.get("trigger_reasons", ())
    lines = [
        "<b>主信号紧急复核</b>",
        f"时间: <b>{escape(observed)} UTC+8</b>",
        f"币种: <b>{escape(conclusion.assessment.symbol)}</b>",
        f"复核状态: <b>{conclusion.tracking_status.value}</b>",
    ]
    if isinstance(reasons, list | tuple):
        lines.extend(f"触发: {escape(str(reason))}" for reason in reasons[:2])
    lines.extend(("", *_conclusion_card(conclusion, emergency=True)))
    return _bounded_html_lines(lines), (conclusion.assessment.symbol,)


def failure_notification(cycle: AnalysisCycleResult) -> tuple[str, str]:
    failures = cycle.diagnostics.get("failures", {})
    code = "ANALYSIS_FAILED"
    if isinstance(failures, dict):
        value = failures.get("CODEX") or failures.get("ANALYSIS")
        if isinstance(value, str) and value:
            code = value.split(":", 1)[0].strip() or code
    generated = cycle.completed_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    text = "\n".join(
        (
            "<b>信号分析暂不可用</b>",
            f"时间: <b>{escape(generated)} UTC+8</b>",
            f"错误类型: <code>{escape(code)}</code>",
            "本轮没有生成交易信号, 也不会用 WATCH 或候选排名填充。",
            "数据扫描仍会保留; 模型恢复后系统将发送恢复通知。",
        )
    )
    return text, code


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
        (
            f"主信号: <b>第 {assessment.selection_rank} 名</b> / "
            f"可信度 <b>{assessment.confidence.value}</b>"
            if assessment.selection_rank is not None and assessment.confidence is not None
            else f"内部评估强度: <b>{assessment.strength.value}</b>"
        ),
        f"方向: <b>{_direction(assessment.direction)}</b>",
        f"市场状态: {escape(assessment.market_state)}",
        f"相比上一轮: {escape(conclusion.comparison_with_previous)}",
        "",
        "<b>综合分析</b>",
        escape(assessment.details[:1200]),
    ]
    if assessment.take_profit is not None:
        lines.extend(
            (
                "",
                f"唯一目标: <b>{assessment.take_profit.value} USDT</b>",
                f"目标依据: {escape(assessment.take_profit.rationale)}",
            )
        )
    outlooks = _outlooks(conclusion)
    if outlooks:
        lines.extend(("", "<b>K 线预测</b>"))
        for label, outlook, forming in outlooks:
            lines.append(_outlook_line(label, outlook, forming=forming, include_window=True))
            lines.append(f"依据: {escape(outlook.rationale[:300])}")
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
        lines.extend(f"• {escape(value[:300])}" for value in assessment.uncertainties[:3])
    lines.extend(("", "<b>工具状态</b>"))
    lines.extend(
        f"• {escape(tool.tool)}: {tool.status.value} — {escape(tool.reason[:180])}"
        for tool in conclusion.tool_assessments
    )
    lines.extend(
        (
            "",
            "证据 ID: <code>"
            f"{escape((', '.join(assessment.evidence_ids) or '无')[:600])}</code>",
        )
    )
    return _bounded_html_lines(lines, suffix="…详情已按 Telegram 限制截断")


def _conclusion_card(
    conclusion: SignalConclusion,
    *,
    emergency: bool = False,
) -> list[str]:
    assessment = conclusion.assessment
    heading = escape(assessment.symbol)
    if not emergency and assessment.selection_rank is not None:
        confidence = assessment.confidence.value if assessment.confidence is not None else "未知"
        heading = f"主信号 {assessment.selection_rank} · {heading} · 可信度 {confidence}"
    result = [
        f"<b>{heading}</b> | {_direction(assessment.direction)}",
        f"参考价: <b>{conclusion.canonical_price.value} USDT</b>",
        f"市场状态: {escape(assessment.market_state)}",
    ]
    if assessment.take_profit is not None:
        result.append(f"大致止盈位: <b>{assessment.take_profit.value} USDT</b>")
    result.extend(
        _outlook_line(label, outlook, forming=forming, include_window=True)
        for label, outlook, forming in _outlooks(conclusion)
    )
    result.extend(
        (
            f"相比上一轮: {escape(conclusion.comparison_with_previous)}",
        )
    )
    if assessment.invalidation is not None:
        result.append(
            f"方向失效: {escape(assessment.invalidation.condition)} "
            f"({assessment.invalidation.reference_price})"
        )
    result.append(escape(assessment.summary[:500]))
    return result


def _direction(direction: Direction | None) -> str:
    if direction is Direction.LONG_BIAS:
        return "偏多"
    if direction is Direction.SHORT_BIAS:
        return "偏空"
    return "无明确方向"


def _outlooks(
    conclusion: SignalConclusion,
) -> tuple[tuple[str, CandleOutlook, bool], ...]:
    assessment = conclusion.assessment
    values = (
        ("形成中 15m", assessment.forming_15m, True),
        ("形成中 30m", assessment.forming_30m, True),
        ("形成中 1h", assessment.forming_1h, True),
        ("下一根 15m", assessment.next_15m, False),
    )
    return tuple(
        (label, outlook, forming)
        for label, outlook, forming in values
        if outlook is not None
    )


def _outlook_line(
    label: str,
    outlook: CandleOutlook,
    *,
    forming: bool,
    include_window: bool,
) -> str:
    suffix = " (分析时)" if forming else ""
    line = (
        f"{label}{suffix}: {_direction(outlook.direction)} / "
        f"{_strength(outlook.strength)}"
    )
    if include_window:
        start = outlook.window_start.astimezone(SHANGHAI).strftime("%H:%M")
        inclusive_end = outlook.window_end - timedelta(minutes=1)
        end = inclusive_end.astimezone(SHANGHAI).strftime("%H:%M")
        line += f" · {start}-{end} UTC+8"
    return line


def _strength(value: str) -> str:
    return {"WEAK": "较弱", "NORMAL": "一般", "STRONG": "较强"}.get(value, value)


def _bounded_html_lines(
    lines: list[str],
    *,
    limit: int = 3900,
    suffix: str = "…内容已按 Telegram 限制截断",
) -> str:
    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    budget = limit - len(suffix) - 1
    kept: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if kept else 0)
        if length + addition > budget:
            break
        kept.append(line)
        length += addition
    return "\n".join(kept).rstrip() + "\n" + suffix
