from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from longtime.market import verify_candles
from longtime.model import Decision
from longtime.risk import Instrument, reachable_tp, sl_price, tp_price


def instrument(tick=".01", step=".001", min_qty=".001", min_notional="5", max_lev="5"):
    return Instrument(
        "TESTUSDT",
        D(tick),
        D(step),
        D(min_qty),
        D(min_notional),
        D(10000),
        D(10000),
        D(max_lev),
        D(".01"),
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_tp_net_costs_and_sl_budget(side):
    q, e, tick, fee, taker, maker = D(".5"), D(100), D(".01"), D(".0275"), D(".00055"), D(".0002")
    tp = tp_price(side, e, q, tick, D("0.5"), fee, maker)
    sl = sl_price(side, e, q, tick, fee, taker)
    sign = 1 if side == "LONG" else -1
    assert D("0.5") <= sign * q * (tp - e) - fee - tp * q * maker - e * q * D(".0005") <= D("0.506")
    assert (
        D("-2.7") <= sign * q * (sl - e) - fee - sl * q * taker - e * q * D(".0005") <= D("-2.69")
    )


@pytest.mark.parametrize("side,high,low", [("LONG", "109", "100"), ("SHORT", "100", "91")])
def test_fixed_half_u_tp_is_reachable(side, high, low):
    now = datetime.now(UTC)
    candles = [
        SimpleNamespace(
            high=D("109" if side == "LONG" else "91"),
            low=D("108" if side == "LONG" else "90"),
            open_time=now - timedelta(minutes=i + 2),
            close_time=now - timedelta(minutes=i + 1),
            completed=True,
            timeframe="1m",
            volume=D(1),
        )
        for i in range(6)
    ]
    result = reachable_tp(
        side, D(100), D(".5"), instrument(), D(".00055"), D(".0002"), D(".01"), candles
    )
    assert result and D(result["target"]) == D("0.5")


def test_below_min_tp_rejected():
    candles = [SimpleNamespace(high=D("100.4"), low=D("99.6"), open_time=datetime.now(UTC))]
    assert (
        reachable_tp(
            "LONG", D(100), D(".5"), instrument(), D(".00055"), D(".0002"), D(".01"), candles
        )
        is None
    )


@pytest.mark.parametrize("lev", ["2", "3", "5", "100"])
def test_leverage_and_margin(lev):
    qty, actual, margin = instrument(max_lev=lev).size(D(100))
    assert actual == min(D(3), D(lev)) and abs(margin - 10) <= D(".5")


def test_exchange_minimum_never_inflates_margin():
    with pytest.raises(ValueError, match="SKIP_SIZE_LIMIT"):
        instrument(min_notional="100").size(D(100))


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "TESTUSDT", "decision": "LONG", "reason": "up", "confidence": 80},
        {"symbol": "TESTUSDT", "decision": "BUY", "reason": "up"},
        {"symbol": "TESTUSDT", "decision": "SHORT"},
        {"symbol": "TESTUSDT", "decision": "LONG", "reason": "up", "leverage": 10},
    ],
)
def test_direction_schema_rejects_natural_language_and_risk_fields(payload):
    with pytest.raises(ValidationError):
        Decision.model_validate(payload)


def test_candle_gap_and_stale_data_rejected():
    now = datetime.now(UTC)
    rows = [SimpleNamespace(open_time=now - timedelta(minutes=10)), SimpleNamespace(open_time=now)]
    with pytest.raises(ValueError, match="interval"):
        verify_candles(rows, 5, now, 2)
    rows = [SimpleNamespace(open_time=now - timedelta(hours=1))]
    with pytest.raises(ValueError, match="Stale"):
        verify_candles(rows, 5, now, 1)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_half_u_target_never_falls_back_to_old_small_target(side):
    candles = [SimpleNamespace(high=D("100.9"), low=D("99.1"), open_time=datetime.now(UTC))]
    assert (
        reachable_tp(
            side, D(100), D(".5"), instrument(), D(".00055"), D(".0002"), D(".01"), candles
        )
        is None
    )


def test_coarse_tick_cannot_inflate_half_u_target():
    candles = [SimpleNamespace(high=D(130), low=D(70), open_time=datetime.now(UTC))]
    assert (
        reachable_tp(
            "LONG",
            D(100),
            D(".5"),
            instrument(tick="1"),
            D(".00055"),
            D(".0002"),
            D(".01"),
            candles,
        )
        is None
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_tp_duration_strict_boundary_and_wicks(side):
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            high=D(110 if side == "LONG" else 90),
            low=D(109 if side == "LONG" else 89),
            open_time=now - timedelta(minutes=2 * i + 2),
            close_time=now - timedelta(minutes=2 * i + 1),
            completed=True,
            timeframe="1m",
            volume=D(1),
        )
        for i in range(6)
    ]

    def check(candles):
        return reachable_tp(
            side, D(100), D(".5"), instrument(), D(".00055"), D(".0002"), D(".01"), candles
        )

    assert check(rows[:5]) is None
    assert check(rows[:5] + rows[:1]) is None
    assert check(rows)["confirmed_seconds"] == 360
    rows[-1].completed = False
    assert check(rows) is None
    rows[-1].completed = True
    rows[-1].low, rows[-1].high = D(80), D(120)
    assert check(rows) is None
    rows[-1].low, rows[-1].high = D(109), D(110)
    rows[-1].open_time -= timedelta(days=2)
    rows[-1].close_time -= timedelta(days=2)
    assert check(rows) is None
