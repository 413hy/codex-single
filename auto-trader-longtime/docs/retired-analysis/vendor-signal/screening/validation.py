from __future__ import annotations

import re
from collections.abc import Sequence

from longtime.vendor.signal.domain.models import EvidenceBundle


def _normalize_redundant_evidence_ids(document: object) -> None:
    """Canonicalize each assessment's redundant citation index.

    Reasons and risks are the authoritative claim-level citations. The top-level
    ``evidence_ids`` field is only an index for storage/audit, so the host may
    deterministically add nested citations before validation. This changes no
    screening decision or prose and all IDs are still checked against the symbol's
    immutable evidence bundle afterwards.
    """

    if not isinstance(document, dict):
        return
    assessments = document.get("assessments")
    if not isinstance(assessments, list):
        return
    for assessment in assessments:
        if not isinstance(assessment, dict):
            continue
        ordered: list[str] = []
        raw_index = assessment.get("evidence_ids")
        if isinstance(raw_index, list):
            ordered.extend(str(item) for item in raw_index if isinstance(item, str))
        for field in ("reasons", "risks"):
            claims = assessment.get(field)
            if not isinstance(claims, list):
                continue
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                citations = claim.get("evidence_ids")
                if isinstance(citations, list):
                    ordered.extend(str(item) for item in citations if isinstance(item, str))
        assessment["evidence_ids"] = list(dict.fromkeys(ordered))


def _sanitize_evidence_citations(document: object, bundles: Sequence[EvidenceBundle]) -> None:
    """Downgrade assessments that cite another symbol's evidence.

    Cross-symbol citations are not safe to repair by guessing. The affected
    assessment is therefore converted to an explicit data-quality abstention;
    valid assessments remain usable and their ranks are compacted afterwards.
    """

    if not isinstance(document, dict) or not isinstance(document.get("assessments"), list):
        return
    allowed_by_symbol = {
        bundle.symbol: {str(item.evidence_id) for item in bundle.evidence_items}
        for bundle in bundles
    }
    for assessment in document["assessments"]:
        if not isinstance(assessment, dict):
            continue
        symbol = str(assessment.get("symbol", ""))
        allowed = allowed_by_symbol.get(symbol, set())
        nested_ids: list[str] = []
        for field in ("reasons", "risks"):
            claims = assessment.get(field)
            if not isinstance(claims, list):
                continue
            for claim in claims:
                if isinstance(claim, dict) and isinstance(claim.get("evidence_ids"), list):
                    nested_ids.extend(str(item) for item in claim["evidence_ids"])
        if not any(item not in allowed for item in nested_ids):
            assessment["evidence_ids"] = [
                item for item in assessment.get("evidence_ids", []) if item in allowed
            ]
            continue
        fallback_id = next(iter(allowed), f"{symbol}.FILTER.CONTEXT")
        assessment.clear()
        assessment.update(
            {
                "symbol": symbol,
                "status": "INSUFFICIENT_DATA",
                "selection_rank": None,
                "value": "UNDETERMINED",
                "value_summary": "证据引用超出本币种数据包,本轮无法可靠判断人工复核价值。",
                "reasons": [
                    {
                        "category": "DATA_QUALITY",
                        "statement": "模型引用了不属于本币种证据包的 ID,已安全降级。",
                        "evidence_ids": [fallback_id],
                    }
                ],
                "risks": [
                    {
                        "category": "SOURCE_CONFLICT",
                        "severity": "HIGH",
                        "statement": "证据归属冲突使该 assessment 不可用于排序。",
                        "evidence_ids": [fallback_id],
                    }
                ],
                "uncertainties": ["证据 ID 归属冲突"],
                "ranking_rationale": "未进入人工复核优先级。",
                "evidence_ids": [fallback_id],
            }
        )
    ranks = sorted(
        (
            item
            for item in document["assessments"]
            if isinstance(item, dict) and item.get("selection_rank") is not None
        ),
        key=lambda item: int(item["selection_rank"]),
    )
    for rank, assessment in enumerate(ranks[:3], start=1):
        assessment["selection_rank"] = rank


def _enforce_selection_quality_gate(document: object) -> None:
    """Remove selections that fail the absolute manual-review quality gate."""

    if not isinstance(document, dict) or not isinstance(document.get("assessments"), list):
        return
    downgraded = False
    support_categories = {"ACTIVITY", "PARTICIPATION", "TRADABILITY", "DISTINCTIVENESS"}
    blocking_execution_risks = {"THIN_LIQUIDITY", "WIDE_SPREAD"}
    for assessment in document["assessments"]:
        if not isinstance(assessment, dict) or assessment.get("status") != "SELECTED":
            continue
        reasons = assessment.get("reasons")
        categories = (
            {str(reason.get("category")) for reason in reasons if isinstance(reason, dict)}
            if isinstance(reasons, list)
            else set()
        )
        risks = assessment.get("risks")
        has_blocking_execution_risk = (
            any(
                isinstance(risk, dict)
                and risk.get("severity") == "HIGH"
                and risk.get("category") in blocking_execution_risks
                for risk in risks
            )
            if isinstance(risks, list)
            else False
        )
        if (
            "STRUCTURE_CLARITY" in categories
            and categories & support_categories
            and not has_blocking_execution_risk
        ):
            continue
        downgraded = True
        assessment["status"] = "NOT_SELECTED"
        assessment["selection_rank"] = None
        assessment["value"] = "UNDETERMINED"
        assessment["value_summary"] = "未通过人工复核的绝对质量门,本轮不列入优先复核。"
        assessment["ranking_rationale"] = "结构或执行质量不足,不以相对排名补足名额。"
    if not downgraded:
        return
    selected = sorted(
        (
            item
            for item in document["assessments"]
            if isinstance(item, dict) and item.get("selection_rank") is not None
        ),
        key=lambda item: int(item["selection_rank"]),
    )
    for rank, assessment in enumerate(selected[:3], start=1):
        assessment["selection_rank"] = rank
    document["selection_summary"] = (
        "已按结构清晰度与市场质量的绝对门槛复核全部六个候选;"
        "最终数量以 assessments 中的连续 rank 为准。"
    )


_FORBIDDEN_SCREENING_TERM_REPLACEMENTS = {
    "紧急复核": "额外复核",
    "参考价": "跨源一致性观测",
    "当前价": "最新观测值",
    "目标价": "目标观测值",
    "止盈": "正向结果边界",
    "止损": "风险观察边界",
    "看涨": "市场判断",
    "看跌": "市场判断",
    "做多": "人工复核",
    "做空": "人工复核",
    "上涨": "波动变化",
    "下跌": "波动变化",
    "方向": "市场判断",
    "预测": "判断",
    "未来": "后续",
    "入场": "纳入复核",
    "出场": "不再复核",
    "失效": "不再适用",
    "阈值": "观察边界",
    "监控": "跟踪",
    "回测": "历史检验",
    "交易": "人工复核",
    "订单": "外部操作",
    "保证收益": "不受支持的确定性",
    "收益": "观察表现",
    "杠杆": "外部风险设置",
    "仓位": "外部规模设置",
    "价格": "数值观测",
    "买入": "人工复核",
    "卖出": "人工复核",
    "多头": "市场状态",
    "空头": "市场状态",
    "账户": "外部信息",
    "余额": "外部信息",
    "保证金": "外部风险设置",
    "持仓": "外部规模设置",
    "开仓": "人工复核",
    "平仓": "人工复核",
    "开多": "人工复核",
    "开空": "人工复核",
    "平多": "人工复核",
    "平空": "人工复核",
    "追多": "人工复核",
    "追空": "人工复核",
    "多单": "外部操作",
    "空单": "外部操作",
    "加仓": "外部规模设置",
    "减仓": "外部规模设置",
    "下单": "外部操作",
    "挂单": "外部操作",
    "市价": "数值观测",
    "限价": "数值观测",
    "爆仓": "外部风险",
    "盈亏": "外部结果",
    "盈利": "外部结果",
    "亏损": "外部风险",
    "触发": "观察",
    "生命周期": "记录周期",
    "策略调优": "配置复核",
    "保证回报": "不受支持的确定性",
    "价位": "数值观测",
}

_FORBIDDEN_SCREENING_ENGLISH_REPLACEMENTS = {
    "take-profit": "outcome boundary",
    "take profit": "outcome boundary",
    "stop-loss": "risk boundary",
    "stop loss": "risk boundary",
    "emergency review": "additional review",
    "strategy tuning": "configuration review",
    "guaranteed return": "unsupported certainty",
    "guaranteed returns": "unsupported certainty",
    "bullish": "observable",
    "bearish": "observable",
    "directional": "descriptive",
    "direction": "market state",
    "bias": "pattern",
    "long": "review",
    "short": "review",
    "buy": "review",
    "sell": "review",
    "entry": "review stage",
    "exit": "review stage",
    "target": "review objective",
    "price": "observed value",
    "forecast": "assessment",
    "predict": "assess",
    "prediction": "assessment",
    "future": "subsequent",
    "horizon": "window",
    "stop": "risk boundary",
    "level": "observation",
    "invalidation": "applicability",
    "trigger": "observation",
    "threshold": "boundary",
    "thresholds": "boundaries",
    "monitoring": "observation",
    "lifecycle": "record",
    "backtest": "historical check",
    "backtesting": "historical check",
    "trade": "review",
    "trading": "review",
    "account": "external context",
    "balance": "external context",
    "margin": "external risk setting",
    "order": "external action",
    "position": "external exposure",
    "leverage": "external risk setting",
    "pnl": "external outcome",
    "profit": "external outcome",
    "loss": "external risk",
    "return": "outcome",
    "returns": "outcomes",
}


def _neutralize_forbidden_screening_terms(document: object) -> None:
    """Keep user-visible screening prose free of legacy trade semantics."""

    if not isinstance(document, dict):
        return
    for key in ("selection_summary", "assessments"):
        value = document.get(key)
        if key == "selection_summary" and isinstance(value, str):
            document[key] = _neutralize_text(value)
        elif isinstance(value, list):
            for assessment in value:
                if not isinstance(assessment, dict):
                    continue
                for field in ("value_summary", "ranking_rationale", "uncertainties"):
                    content = assessment.get(field)
                    if isinstance(content, str):
                        assessment[field] = _neutralize_text(content)
                    elif isinstance(content, list):
                        assessment[field] = [
                            _neutralize_text(item) if isinstance(item, str) else item
                            for item in content
                        ]
                for field in ("reasons", "risks"):
                    claims = assessment.get(field)
                    if not isinstance(claims, list):
                        continue
                    for claim in claims:
                        if isinstance(claim, dict) and isinstance(claim.get("statement"), str):
                            claim["statement"] = _neutralize_text(claim["statement"])


def _neutralize_text(value: str) -> str:
    for source, replacement in _FORBIDDEN_SCREENING_TERM_REPLACEMENTS.items():
        value = value.replace(source, replacement)
    value = value.replace("_", " ")
    for source, replacement in _FORBIDDEN_SCREENING_ENGLISH_REPLACEMENTS.items():
        value = re.sub(rf"\b{re.escape(source)}\b", replacement, value, flags=re.IGNORECASE)
    return value
