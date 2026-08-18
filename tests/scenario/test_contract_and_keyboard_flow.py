from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from bybit_signal.domain.enums import (
    Comparator,
    Direction,
    MonitoringMetric,
    PriceType,
    SignalConfidence,
    SignalStrength,
    ToolStatus,
    TrackingStatus,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    CanonicalPrice,
    FormingHourOutlook,
    InvalidationCondition,
    MonitoringDirective,
    PriceLevel,
    SignalConclusion,
    ToolAssessment,
)
from bybit_signal.notifications.keyboards import (
    back_to_cycle_keyboard,
    details_keyboard,
    main_reply_keyboard,
)


def test_cys_strong_signal_contract_reaches_notification_keyboards() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    observed_at = datetime(2026, 8, 17, 15, 48, 7, tzinfo=shanghai)
    evidence_id = "CMI.CYSUSDT.BYBIT.5M.001"
    assessment = CandidateAssessment(
        symbol="CYSUSDT",
        strength=SignalStrength.STRONG,
        selection_rank=1,
        confidence=SignalConfidence.HIGH,
        direction=Direction.SHORT_BIAS,
        market_state="趋势下跌中的超卖反抽 / 低位修复",
        take_profit=PriceLevel(
            value=Decimal("0.5780"),
            rationale="已确认的5m下方结构与成交密集区",
            evidence_ids=(evidence_id,),
        ),
        forming_15m=FormingHourOutlook(
            direction=Direction.SHORT_BIAS,
            strength="NORMAL",
            window_start=observed_at.replace(minute=45, second=0),
            window_end=observed_at.replace(minute=0, second=0) + timedelta(hours=1),
            rationale="形成中15m仍由短周期空向主导",
        ),
        forming_30m=FormingHourOutlook(
            direction=Direction.SHORT_BIAS,
            strength="NORMAL",
            window_start=observed_at.replace(minute=30, second=0),
            window_end=observed_at.replace(minute=0, second=0) + timedelta(hours=1),
            rationale="形成中30m保持下跌修复结构",
        ),
        forming_1h=FormingHourOutlook(
            direction=Direction.SHORT_BIAS,
            strength="NORMAL",
            window_start=observed_at.replace(minute=0, second=0),
            window_end=observed_at.replace(minute=0, second=0) + timedelta(hours=1),
            rationale="15m与1h结构仍然偏空",
        ),
        next_15m=FormingHourOutlook(
            direction=Direction.SHORT_BIAS,
            strength="NORMAL",
            window_start=observed_at.replace(minute=0, second=0) + timedelta(hours=1),
            window_end=observed_at.replace(minute=15, second=0) + timedelta(hours=1),
            rationale="下一根15m预计继续测试下方目标",
        ),
        invalidation=InvalidationCondition(
            condition="价格重新收复并在5m/15m稳定站上0.6230",
            reference_price=Decimal("0.6230"),
            evidence_ids=(evidence_id,),
        ),
        summary="当前上涨仍理解为空头趋势中的反抽。",
        details="参考价格来自 Bybit. 多交易所订单流和完成 K 线共同支持当前判断。",
        evidence_ids=(evidence_id,),
        monitoring_directives=(
            MonitoringDirective(
                family_id="cys.price.invalidation.short",
                metric=MonitoringMetric.LAST_PRICE,
                comparator=Comparator.GREATER_THAN,
                threshold=Decimal("0.6230"),
                hysteresis=Decimal("0.0020"),
                valid_for_seconds=3600,
                reason="观察空头结构失效",
                evidence_ids=(evidence_id,),
            ),
        ),
    )
    conclusion = SignalConclusion(
        analysis_id="cycle_20260817_1530",
        generated_at=observed_at,
        canonical_price=CanonicalPrice(
            symbol="CYSUSDT",
            price_type=PriceType.LAST,
            value=Decimal("0.6041"),
            timestamp=observed_at,
        ),
        assessment=assessment,
        tracking_status=TrackingStatus.NEW,
        comparison_with_previous="首次出现强信号",
        tool_assessments=(
            ToolAssessment(
                tool="CMI",
                status=ToolStatus.AVAILABLE,
                version="2.2.1",
                reason="Bybit、Binance、OKX公开证据可用",
                evidence_ids=(evidence_id,),
            ),
            ToolAssessment(
                tool="TradingAgents",
                status=ToolStatus.UNAVAILABLE_FOR_INSTRUMENT,
                version="a33fd4c",
                reason="CYS不在该低频研究工具的可靠资产覆盖内",
            ),
        ),
    )

    assert conclusion.assessment.direction is Direction.SHORT_BIAS
    assert conclusion.assessment.take_profit is not None
    assert conclusion.assessment.take_profit.value == Decimal("0.5780")
    assert conclusion.assessment.next_15m is not None
    assert main_reply_keyboard()["is_persistent"] is False
    callback = details_keyboard(conclusion.analysis_id, conclusion.assessment.symbol)
    assert callback["inline_keyboard"][0][0]["text"] == "查看分析详情"
    assert back_to_cycle_keyboard(conclusion.analysis_id)["inline_keyboard"][0][0][
        "text"
    ] == "⬅️ 返回本轮信号"
