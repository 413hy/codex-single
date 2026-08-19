from __future__ import annotations

import json
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from bybit_signal.domain.enums import (
    Comparator,
    CycleMode,
    CycleStatus,
    Direction,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    AnalysisCycleResult,
    CandleOutlook,
    FailureEvent,
    RetryJob,
    SignalConclusion,
)
from bybit_signal.monitoring.engine import ThresholdEvent

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
        and conclusion.tracking_status in {TrackingStatus.EXITED, TrackingStatus.INVALIDATED}
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


def failure_notification(event: FailureEvent) -> str:
    generated = event.occurred_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    completed = "、".join(event.completed_steps) if event.completed_steps else "无可确认的完整阶段"
    lines = [
        f"<b>流程异常 · {_workflow_label(event)}</b>",
        f"时间: <b>{escape(generated)} UTC+8</b>",
        f"分析 ID: <code>{escape(event.analysis_id)}</code>",
    ]
    if event.symbol is not None:
        lines.append(f"币种: <b>{escape(event.symbol)}</b>")
    lines.extend(
        (
            f"异常位置: <b>第 {event.step_index}/{event.total_steps} 步 · "
            f"{escape(_step_label(event))}</b>",
            f"此前已完成: {escape(completed)}",
            f"失败代码: <code>{escape(event.code)}</code>",
            f"直接原因: {escape(event.direct_cause)}",
            "",
            f"影响: {escape(event.impact)}",
            f"解决方式: {escape(event.resolution)}",
        )
    )
    return _bounded_html_lines(lines, suffix="…摘要已截断, 请查看完整诊断链")


def failure_diagnostic_notification(event: FailureEvent, job: RetryJob) -> str:
    lines = [
        f"<b>完整诊断链 · {_workflow_label(event)}</b>",
        f"失败 ID: <code>{escape(event.failure_id)}</code>",
        f"分析 ID: <code>{escape(event.analysis_id)}</code>",
        f"重试状态: <b>{escape(job.status.value)}</b>",
        f"阶段: {event.step_index}/{event.total_steps} · {escape(_step_label(event))}",
        "",
        "<b>因果链</b>",
    ]
    lines.extend(
        f"{index}. {escape(value)}"
        for index, value in enumerate(event.causal_chain, start=1)
    )
    lines.extend(
        (
            "",
            "<b>实际影响</b>",
            escape(event.impact),
            "",
            "<b>推荐解决方式</b>",
            escape(event.resolution),
        )
    )
    if job.error:
        lines.extend(("", "<b>最近一次重试错误</b>", escape(job.error)))
    if event.safe_diagnostics:
        payload = json.dumps(
            event.safe_diagnostics,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
        lines.extend(("", "<b>安全诊断数据</b>", f"<pre>{escape(payload[:1800])}</pre>"))
    return _bounded_html_lines(lines, suffix="…诊断内容已按 Telegram 限制截断")


def retry_status_notification(
    event: FailureEvent,
    job: RetryJob,
    message: str,
) -> str:
    return "\n".join(
        (
            f"<b>异常流程重试 · {_workflow_label(event)}</b>",
            f"分析 ID: <code>{escape(event.analysis_id)}</code>",
            f"状态: <b>{escape(job.status.value)}</b>",
            escape(message),
        )
    )


def threshold_trigger_notification(event: ThresholdEvent) -> str:
    observed = event.observed_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    level = "关键" if event.critical else "结构"
    conditions = event.matched_conditions
    if not conditions:
        condition_lines = [
            f"• <code>{event.metric.value}</code>: "
            f"<b>{event.observed_value}</b> / 阈值 <b>{event.threshold}</b>"
        ]
    else:
        condition_lines = [
            f"• <code>{condition.metric.value}</code>: "
            f"<b>{condition.observed_value}</b> "
            f"{'&gt;' if condition.comparator is Comparator.GREATER_THAN else '&lt;'} "
            f"<b>{condition.threshold}</b>"
            for condition in conditions
        ]
    return "\n".join(
        [
            f"<b>{level}阈值已触发 · {escape(event.symbol)}</b>",
            f"时间: <b>{escape(observed)} UTC+8</b>",
            "完整触发条件:",
            *condition_lines,
            f"含义: {escape(event.reason[:500])}",
            "旧阈值已失效; 正在重新采集最新数据并进行紧急复核, 正式结论随后发送。",
        ]
    )


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
                f"大致止盈位: <b>{assessment.take_profit.value} USDT</b>",
                "说明: 该价格仅作近端参考, 不参与方向结构失效判断。",
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
            f"证据 ID: <code>{escape((', '.join(assessment.evidence_ids) or '无')[:600])}</code>",
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
    result.extend((f"相比上一轮: {escape(conclusion.comparison_with_previous)}",))
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
        (label, outlook, forming) for label, outlook, forming in values if outlook is not None
    )


def _outlook_line(
    label: str,
    outlook: CandleOutlook,
    *,
    forming: bool,
    include_window: bool,
) -> str:
    suffix = " (分析时)" if forming else ""
    line = f"{label}{suffix}: {_direction(outlook.direction)} / {_strength(outlook.strength)}"
    if include_window:
        start = outlook.window_start.astimezone(SHANGHAI).strftime("%H:%M")
        inclusive_end = outlook.window_end - timedelta(minutes=1)
        end = inclusive_end.astimezone(SHANGHAI).strftime("%H:%M")
        line += f" · {start}-{end} UTC+8"
    return line


def _strength(value: str) -> str:
    return {"WEAK": "较弱", "NORMAL": "一般", "STRONG": "较强"}.get(value, value)


def _workflow_label(event: FailureEvent) -> str:
    return {
        "SCHEDULED_ANALYSIS": "定时方向分析",
        "EMERGENCY_ANALYSIS": "阈值触发紧急复核",
        "MONITORING_SETUP": "实时监测阈值配置",
    }.get(event.workflow.value, event.workflow.value)


def _step_label(event: FailureEvent) -> str:
    return {
        "MARKET_SCAN": "全市场扫描与 Top5 过滤",
        "EVIDENCE_COLLECTION": "深度数据采集",
        "MODEL_ANALYSIS": "模型方向分析",
        "FRESHNESS_CHECK": "发送前新鲜度校验",
        "MONITORING_REVIEW_COLLECTION": "监测复核数据采集",
        "MODEL_MONITORING_REVIEW": "模型阈值审核/重写",
        "MECHANICAL_ACTIVATION": "机械校验与版本发布",
        "UNKNOWN": "未捕获阶段边界",
    }.get(event.step.value, event.step.value)


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
