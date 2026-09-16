"""Versioned, closed-candle Decimal evidence; no trade thresholds or decisions."""

from decimal import Decimal as D

from analysis_core.vendor.models import Candle


def ema(values: list[D], period: int) -> list[D]:
    """SMA seed followed by conventional EMA; output starts at period - 1."""
    current = sum(values[:period], D(0)) / period
    result = [current]
    alpha = D(2) / (period + 1)
    for value in values[period:]:
        current += alpha * (value - current)
        result.append(current)
    return result


def wilder(values: list[D], period: int) -> D:
    current = sum(values[:period], D(0)) / period
    for value in values[period:]:
        current = (current * (period - 1) + value) / period
    return current


def structure(rows: list[Candle]) -> dict:
    last, first = rows[-1], rows[0]
    low, high = min(r.low for r in rows), max(r.high for r in rows)
    mean = sum((r.volume for r in rows[:-1]), D(0)) / (len(rows) - 1) if len(rows) > 1 else None
    return {
        "samples": len(rows),
        "start_at": first.open_time.isoformat(),
        "end_at": last.close_time.isoformat(),
        "high": high,
        "low": low,
        "close_change_ratio": (last.close - first.close) / first.close if len(rows) > 1 else None,
        "close_position": (last.close - low) / (high - low) if high != low else None,
        "volume_ratio_previous_available": last.volume / mean if mean else None,
    }


def snapshot(rows: list[Candle]) -> dict:
    closes = [r.close for r in rows]
    last = rows[-1]
    n = len(rows)
    macd_result = None
    if n >= 34 and last.timeframe != "2h":
        fast, slow = ema(closes, 12), ema(closes, 26)
        macd = [a - b for a, b in zip(fast[14:], slow, strict=True)]
        signal = ema(macd, 9)[-1]
        macd_result = {"line": macd[-1], "signal": signal, "histogram": macd[-1] - signal}
    rsi = atr = None
    if n >= 15:
        changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
        gain = wilder([max(x, D(0)) for x in changes], 14)
        loss = wilder([max(-x, D(0)) for x in changes], 14)
        rsi = D(50) if gain == loss == 0 else D(100) if loss == 0 else 100 - 100 / (1 + gain / loss)
        tr = [
            max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
            for a, b in zip(rows, rows[1:], strict=False)
        ]
        atr = wilder(tr, 14)
    bollinger = range20 = None
    if n >= 20:
        middle = sum(closes[-20:], D(0)) / 20
        deviation = (sum(((x - middle) ** 2 for x in closes[-20:]), D(0)) / 20).sqrt()
        lower, upper = middle - 2 * deviation, middle + 2 * deviation
        bollinger = {
            "middle": middle,
            "lower": lower,
            "upper": upper,
            "bandwidth_ratio": (upper - lower) / middle,
            "percent_b": (last.close - lower) / (upper - lower) if upper != lower else None,
        }
        low, high = min(r.low for r in rows[-20:]), max(r.high for r in rows[-20:])
        range20 = {
            "low": low,
            "high": high,
            "close_position": (last.close - low) / (high - low) if high != low else None,
        }
    baseline_volume = sum((r.volume for r in rows[-21:-1]), D(0)) / 20 if n >= 21 else None
    return {
        "background_structure": structure(rows),
        "closed_at": last.close_time.isoformat(),
        "close": last.close,
        "ema9": ema(closes, 9)[-1] if n >= 9 else None,
        "ema20": ema(closes, 20)[-1] if n >= 20 else None,
        "rsi14": rsi,
        "macd_12_26_9": macd_result,
        "atr14": atr,
        "bollinger_20_2": bollinger,
        "volume_ratio_previous20": last.volume / baseline_volume if baseline_volume else None,
        "range20": range20,
    }


def direction_indicators(rows: list[Candle]) -> dict:
    closed = [r for r in rows if r.completed]
    if not closed:
        raise ValueError("No closed candles for direction analysis")
    windows = {
        "2h": (3, 6),
        "1h": (4, 8),
        "30m": (6, 12),
        "15m": (6, 12),
        "10m": (6, 12),
        "5m": (6, 12),
        "3m": (5, 10),
        "1m": (5, 15),
    }[closed[-1].timeframe]
    return {
        "recent_windows": [
            {
                "requested_samples": count,
                "complete": len(closed) >= count,
                **structure(closed[-count:]),
            }
            for count in windows
        ],
        "indicator_notes": {"macd_12_26_9": "2h MACD继续省略，周内量价结构优先，不依赖该辅助指标"}
        if closed[-1].timeframe == "2h"
        else {},
        "version": "layered-indicators-v3",
        "closed_samples": len(closed),
        "forming_excluded": len(rows) - len(closed),
        "recent": [
            snapshot(closed[:end]) for end in range(max(1, len(closed) - 2), len(closed) + 1)
        ],
    }


def price_volume(rows: list[Candle]) -> dict:
    """OHLCV evidence only: never infer aggressor flow or volume-at-price."""
    closed = [c for c in rows if c.completed]
    groups: dict[str, list[Candle]] = {}
    for c in closed:
        groups.setdefault(c.open_time.date().isoformat(), []).append(c)

    def summary(items):
        volume = sum((c.volume for c in items), D(0))
        turnover = sum((c.turnover for c in items), D(0))
        return {
            **structure(items),
            "volume": volume,
            "turnover": turnover,
            "vwap": turnover / volume if volume else None,
            "coverage_seconds": sum(
                int((c.close_time - c.open_time).total_seconds()) for c in items
            ),
            "up_candle_volume": sum((c.volume for c in items if c.close > c.open), D(0)),
            "down_candle_volume": sum((c.volume for c in items if c.close < c.open), D(0)),
        }

    return {
        "method": "OHLCV only; candle direction is not aggressor side; high-volume ranges are not volume-at-price",
        "daily": [{"utc_date": day, **summary(items)} for day, items in groups.items()],
        "high_volume_candles": [
            {
                "open_time": c.open_time.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "turnover": c.turnover,
                "body_range_ratio": abs(c.close - c.open) / (c.high - c.low)
                if c.high != c.low
                else None,
            }
            for c in sorted(closed, key=lambda c: c.volume, reverse=True)[:10]
        ],
    }
