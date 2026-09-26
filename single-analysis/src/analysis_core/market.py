import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from analysis_core.indicators import direction_indicators, price_volume
from analysis_core.orderflow import collect_orderflow
from analysis_core.tradingview import TradingViewWeb
from analysis_core.tradingview_discovery import TradingViewDiscovery
from analysis_core.tradingview_sources import CoinContextSource, NewsPageSource, SourceRegistry
from analysis_core.vendor.models import Candle
from analysis_core.vendor.public import BybitPublicClient


def verify_candles(rows, minutes, now, minimum):
    if len(rows) < minimum:
        raise ValueError("Incomplete market candle window")
    duration = timedelta(minutes=minutes)
    if rows[-1].open_time > now + timedelta(seconds=5):
        raise ValueError("Future market candles")
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
            or (not row.completed and index != len(rows) - 1)
        ):
            raise ValueError("Invalid direction candle identity/OHLCV/completion")


class Markets:
    def __init__(self, client=None, *, include_orderflow=True, include_tradingview=False,
                 tradingview_extra_fields=""):
        self.client = client or BybitPublicClient(max_attempts=1)
        self.include_orderflow = include_orderflow
        self.tradingview = TradingViewWeb(extra_fields=tradingview_extra_fields) if include_tradingview else None
        self.discovery = TradingViewDiscovery(self.tradingview) if self.tradingview else None
        self.tv_sources = (
            SourceRegistry((CoinContextSource(self.tradingview), NewsPageSource()))
            if self.tradingview else None
        )

    async def discover(self):
        if self.discovery is None:
            raise ValueError("TradingView discovery is required")
        instruments = await self.client.instruments()
        tradable = {
            item.symbol for item in instruments
            if item.status == "Trading" and item.contract_type == "LinearPerpetual"
            and item.quote_coin == "USDT" and item.settle_coin == "USDT"
        }
        return await self.discovery.collect(tradable)

    async def tradingview_evidence(self, candidate):
        if self.tradingview is None:
            raise ValueError("TradingView evidence is required")
        assert self.tv_sources is not None
        result = await self.tradingview.collect(
            candidate["symbol"], market_rankings=candidate.get("market_rankings")
        )
        return await self.tv_sources.enrich(candidate["symbol"], result, stage="initial")

    async def evidence(self, symbol, candidate):
        now = datetime.now(UTC)
        specs = (
            ("30m", 30, 337),
            ("1h", 60, 169),
            ("2h", 120, 85),
            ("1m", 1, 721),
            ("3m", 3, 241),
            ("5m", 5, 146),
            ("15m", 15, 673),
        )
        batches = await asyncio.gather(
            *(
                self.client.recent_candles(symbol, timeframe=tf, limit=count)  # type: ignore[arg-type]
                for tf, _, count in specs
            )
        )
        now = datetime.now(UTC)
        data = {}
        indicators = {}
        for (tf, minutes, _count), rows in zip(specs, batches, strict=True):
            validate_direction_rows(rows, symbol, tf, minutes, now)
            verify_candles(rows, minutes, now, 1)
            if tf in {"15m", "30m", "1h"}:
                latest = next((c for c in reversed(rows) if c.completed), None)
                if latest is None or now - latest.close_time > timedelta(minutes=minutes + 2):
                    raise ValueError("SKIP_STALE_BYBIT_DIRECTION_HISTORY: " + tf)
            minimum_closed = {"15m": 48, "30m": 24, "1h": 12, "2h": 6}.get(tf)
            if minimum_closed is not None and sum(c.completed for c in rows) < minimum_closed:
                raise ValueError("SKIP_INSUFFICIENT_DIRECTION_HISTORY: " + tf)
            visible = list(rows[-145:]) if tf == "5m" else list(rows)
            data[tf] = [c.model_dump(mode="json") for c in visible]
            indicators[tf] = direction_indicators(visible)
        ten_minute = aggregate_ten_minutes(
            dict(zip((tf for tf, _, _ in specs), batches, strict=True))["5m"]
        )
        verify_candles(ten_minute, 10, now, 1)
        data["10m"] = [c.model_dump(mode="json") for c in ten_minute[-73:]]
        indicators["10m"] = direction_indicators(ten_minute[-73:])
        orderflow = await collect_orderflow(self.client, symbol) if self.include_orderflow else None
        reference = candidate.get("tradingview")
        if reference is None and self.tradingview:
            reference = await self.tradingview.collect(symbol)
        if reference is not None and self.tv_sources:
            reference = await self.tv_sources.enrich(symbol, reference, stage="final")
        return {
            **({"tradingview": reference} if reference is not None else {}),
            "orderflow": orderflow,
            "history_hours": {
                tf: round(len(rows) * minutes / 60, 2)
                for (tf, minutes, _), rows in zip(specs, batches, strict=True)
            },
            "indicators": indicators,
            "price_volume": {
                tf: price_volume(list(rows))
                for (tf, _, _), rows in zip(specs, batches, strict=True)
                if tf in {"15m", "30m", "1h", "2h"}
            },
            "indicator_contract": "direction-1-2h-v1：15m/30m/1h近期已收盘量价为主，2h辅助；近一周结构仅作背景，不要求完整一周。1m/3m/5m/10m用于入场节奏。统计仅用已收盘数据，量价统计不是订单流或逐价成交分布。",
            "unavailable_evidence": [
                "footprint",
                "liquidation_heatmap",
                "large_order_tracking",
                "historical_aggressor_delta",
                "complete_volume_at_price",
                "continuous_dom_and_cancellations",
                "diagonal_stacked_imbalance",
            ],
            "timeframe_roles": {
                "primary": ["15m", "30m", "1h"],
                "core": ["15m", "30m", "1h"],
                "secondary": ["1m", "3m", "5m", "10m", "2h"],
                "background": ["4h", "1d", "available_7d_structure"],
            },
            "derived_timeframes": {"10m": "UTC对齐的相邻5m K线聚合；未收盘状态保留"},
            "symbol": symbol,
            "observed_at": now.isoformat(),
            "selection": candidate,
            "candles": data,
        }

    async def reachability(self, symbol):
        rows = await self.client.recent_candles(symbol, timeframe="1m", limit=1441)
        now = datetime.now(UTC)
        validate_direction_rows(rows, symbol, "1m", 1, now)
        verify_candles(rows, 1, now, 1440)
        return tuple(c for c in rows if c.open_time >= now - timedelta(hours=24))

    async def close(self):
        try:
            await self.client.close()
        finally:
            if self.tradingview:
                await self.tradingview.close()


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
