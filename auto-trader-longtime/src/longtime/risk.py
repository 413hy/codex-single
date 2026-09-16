"""Fixed program-controlled economics. Decimal step rounding adapted from bybit-trader."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

D = Decimal
MARGIN = D("10")
RESERVE = D("20")
MAX_LEVERAGE = D("3")
MARGIN_TOLERANCE = D("0.50")
SL_LOSS = D("2.7")
SLIPPAGE = D("0.0005")
TP_TARGETS = (D("0.5"),)
TP_ROUNDING_TOLERANCE = D("0.05")


def floor(value: D, step: D) -> D:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil(value: D, step: D) -> D:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def number(value) -> D:
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite exchange number")
    return result


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick: D
    step: D
    min_qty: D
    min_notional: D
    max_market_qty: D
    max_limit_qty: D
    max_leverage: D
    leverage_step: D

    @classmethod
    def parse(cls, row):
        if (
            row.get("status") != "Trading"
            or row.get("contractType") != "LinearPerpetual"
            or row.get("settleCoin") != "USDT"
            or row.get("quoteCoin") != "USDT"
        ):
            raise ValueError("Not a trading USDT perpetual")
        lot, lev = row["lotSizeFilter"], row["leverageFilter"]
        obj = cls(
            row["symbol"],
            number(row["priceFilter"]["tickSize"]),
            number(lot["qtyStep"]),
            number(lot["minOrderQty"]),
            number(lot.get("minNotionalValue", 0)),
            number(lot["maxMktOrderQty"]),
            number(lot["maxOrderQty"]),
            number(lev["maxLeverage"]),
            number(lev["leverageStep"]),
        )
        if min(obj.tick, obj.step, obj.min_qty, obj.max_leverage, obj.leverage_step) <= 0:
            raise ValueError("Invalid instrument filters")
        return obj

    def size(self, price: D, *, margin_target: D = MARGIN, leverage_target: D = MAX_LEVERAGE):
        leverage = floor(min(leverage_target, self.max_leverage), self.leverage_step)
        if price <= 0 or not D(1) <= leverage_target <= D(5) or leverage <= 0 or margin_target <= 0:
            raise ValueError("Invalid price or leverage")
        minimum = ceil(max(self.min_qty, self.min_notional / price), self.step)
        qty = max(minimum, floor(margin_target * leverage / price, self.step))
        margin = qty * price / leverage
        if abs(margin - margin_target) > MARGIN_TOLERANCE:
            raise ValueError(
                f"SKIP_SIZE_LIMIT: exchange minimum/step cannot fit approximately {margin_target}U"
            )
        if qty > min(self.max_market_qty, self.max_limit_qty):
            raise ValueError("SKIP_SIZE_LIMIT: exchange quantity maximum")
        return qty, leverage, margin


def tp_price(side, entry: D, qty: D, tick: D, target: D, entry_fee: D, exit_rate: D) -> D:
    # Net = direction * qty * (exit-entry) - paid entry fee - expected exit fee - slip.
    sign = D(1 if side == "LONG" else -1)
    price = (sign * qty * entry + target + entry_fee + entry * qty * SLIPPAGE) / (
        qty * (sign - exit_rate)
    )
    rounded = ceil(price, tick) if side == "LONG" else floor(price, tick)
    if rounded <= 0 or sign * (rounded - entry) <= 0:
        raise ValueError("Invalid TP geometry")
    return rounded


def sl_price(side, entry: D, qty: D, tick: D, entry_fee: D, taker: D, loss: D = SL_LOSS) -> D:
    sign = D(1 if side == "LONG" else -1)
    price = (sign * qty * entry - loss + entry_fee + entry * qty * SLIPPAGE) / (
        qty * (sign - taker)
    )
    rounded = max(tick, ceil(price, tick)) if side == "LONG" else floor(price, tick)
    if rounded <= 0 or sign * (rounded - entry) >= 0:
        raise ValueError("Invalid SL geometry")
    return rounded


def reachable_tp(
    side,
    price: D,
    qty: D,
    instrument: Instrument,
    taker: D,
    maker: D,
    spread: D,
    candles,
    *,
    targets=TP_TARGETS,
):
    now = datetime.now(UTC)
    # One tick or a quarter spread, capped at two ticks: deliberately light buffer.
    buffer = max(instrument.tick, min(spread / 4, instrument.tick * 2))
    for target in targets:
        tp = tp_price(side, price, qty, instrument.tick, target, price * qty * taker, maker)
        sign = D(1 if side == "LONG" else -1)
        projected = (
            sign * qty * (tp - price)
            - price * qty * taker
            - tp * qty * maker
            - price * qty * SLIPPAGE
        )
        if projected > target + TP_ROUNDING_TOLERANCE:
            continue
        hits = []
        seen = set()
        for c in candles:
            if (
                c.open_time in seen
                or not getattr(c, "completed", False)
                or getattr(c, "timeframe", None) != "1m"
                or c.close_time - c.open_time != timedelta(minutes=1)
                or c.open_time < now - timedelta(hours=24)
                or c.close_time > now
                or c.volume <= 0
            ):
                continue
            seen.add(c.open_time)
            if c.low >= tp + buffer if side == "LONG" else c.high <= tp - buffer:
                hits.append(c)
        seconds = len(hits) * 60
        if seconds > 300:
            return {
                "target": str(target),
                "price": str(tp),
                "buffer": str(buffer),
                "last_touch_at": max(c.open_time for c in hits).isoformat(),
                "confirmed_seconds": seconds,
                "duration_method": "completed_1m_entire_range_beyond_buffer_lower_bound",
            }
    return None
