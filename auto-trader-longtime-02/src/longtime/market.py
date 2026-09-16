from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from longtime.vendor.models import Candle
from longtime.vendor.public import BybitPublicClient


def verify_candles(rows, minutes, now, minimum):
    if len(rows) < minimum:
        raise ValueError("Incomplete market candle window")
    duration = timedelta(minutes=minutes)
    if now - rows[-1].open_time > duration + timedelta(seconds=45) or rows[
        -1
    ].open_time > now + timedelta(seconds=5):
        raise ValueError("Stale or future market candles")
    if any(b.open_time - a.open_time != duration for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError("Missing or duplicate market candle intervals")


def validate_direction_rows(rows, symbol, timeframe, minutes, now):
    for index, row in enumerate(rows):
        values = [row.open, row.high, row.low, row.close, row.volume, row.turnover]
        if (
            row.symbol != symbol
            or row.timeframe != timeframe
            or any(not value.is_finite() for value in values)
            or min(values[:4]) <= 0
            or min(values[4:]) < 0
            or row.high < max(row.open, row.close)
            or row.low > min(row.open, row.close)
            or row.low > row.high
            or type(row.completed) is not bool
            or row.close_time != row.open_time + timedelta(minutes=minutes)
            or int(row.open_time.timestamp()) % (minutes * 60) != 0
            or (row.completed and row.close_time > now)
            or (
                not row.completed
                and (index != len(rows) - 1 or row.close_time < now - timedelta(seconds=45))
            )
        ):
            raise ValueError("Invalid direction candle identity/OHLCV/completion")


class Markets:
    def __init__(self, client=None):
        self.client = client or BybitPublicClient()

    async def reachability(self, symbol):
        rows = await self.client.recent_candles(symbol, timeframe="1m", limit=1441)
        now = datetime.now(UTC)
        validate_direction_rows(rows, symbol, "1m", 1, now)
        verify_candles(rows, 1, now, 1440)
        return tuple(c for c in rows if c.open_time >= now - timedelta(hours=24))

    async def close(self):
        await self.client.close()


def aggregate_ten_minutes(rows):
    """Bybit has no native 10m interval; aggregate only contiguous, UTC-aligned 5m rows."""
    groups: dict[int, list[Candle]] = {}
    for candle in rows:
        bucket = int(candle.open_time.timestamp()) // 600 * 600
        groups.setdefault(bucket, []).append(candle)
    result = []
    for bucket, candles in sorted(groups.items()):
        start = datetime.fromtimestamp(bucket, UTC)
        if candles[0].open_time != start:
            if bucket == min(groups):
                continue  # The request may begin in the second half of an older bucket.
            raise ValueError("10m aggregation missing first 5m candle")
        if len(candles) > 2 or any(
            c.timeframe != "5m"
            or c.symbol != candles[0].symbol
            or c.open_time != start + timedelta(minutes=5 * index)
            for index, c in enumerate(candles)
        ):
            raise ValueError("10m aggregation has inconsistent 5m candles")
        if len(candles) != 2 and bucket != max(groups):
            raise ValueError("10m aggregation missing second 5m candle")
        result.append(
            Candle(
                symbol=candles[0].symbol,
                timeframe="10m",
                open_time=start,
                close_time=start + timedelta(minutes=10),
                open=candles[0].open,
                high=max(c.high for c in candles),
                low=min(c.low for c in candles),
                close=candles[-1].close,
                volume=sum((c.volume for c in candles), D(0)),
                turnover=sum((c.turnover for c in candles), D(0)),
                completed=len(candles) == 2 and all(c.completed for c in candles),
                source="BYBIT",
            )
        )
    return result
