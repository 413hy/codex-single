from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bybit_signal.domain.enums import (
    Direction,
    MarketType,
    SignalStrength,
)
from bybit_signal.domain.models import (
    CandidateAssessment,
    Candle,
)


def test_candle_requires_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Candle(
            symbol="CYSUSDT",
            timeframe="5m",
            open_time=datetime(2026, 8, 17, 8, 0),
            close_time=datetime(2026, 8, 17, 8, 5),
            open=Decimal("0.60"),
            high=Decimal("0.61"),
            low=Decimal("0.59"),
            close=Decimal("0.605"),
            volume=Decimal("100"),
            turnover=Decimal("60"),
            completed=True,
            source="BYBIT",
        )


def test_strong_assessment_requires_complete_direction_contract() -> None:
    with pytest.raises(ValidationError, match="requires one direction"):
        CandidateAssessment(
            symbol="CYSUSDT",
            strength=SignalStrength.STRONG,
            market_state="下跌趋势中的反抽",
            summary="当前结构仍然偏空。",
            details="多周期结构与订单流仍然支持偏空。",
            evidence_ids=("BYBIT.CYS.5M.001",),
        )


def test_non_strong_assessment_does_not_invent_trade_levels() -> None:
    assessment = CandidateAssessment(
        symbol="CYSUSDT",
        strength=SignalStrength.NO_STRONG_SIGNAL,
        market_state="震荡且方向证据冲突",
        summary="当前没有足够强的单一方向。",
        details="等待完成柱和订单流提供新的结构证据。",
    )
    assert assessment.direction is None
    assert assessment.take_profit is None


def test_market_type_contract_has_no_account_semantics() -> None:
    assert set(MarketType) == {MarketType.LINEAR_PERPETUAL, MarketType.SPOT}
    assert set(Direction) == {Direction.LONG_BIAS, Direction.SHORT_BIAS}
    assert Decimal("0.6041") > 0
