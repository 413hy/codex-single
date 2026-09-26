"""Discover tradable Bybit perpetuals from TradingView's public CEX screener."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

from analysis_core.tradingview import (
    RANK_COLUMNS,
    RANK_METRICS,
    TradingViewError,
    normalize_rankings,
)

SCREENER_URL = "https://scanner.tradingview.com/crypto/scan"
SCREENER_PAGE = "https://www.tradingview.com/crypto-screener/"
SYMBOL_RE = re.compile(r"BYBIT:([A-Z0-9]{1,24}USDT)\.P")


class TradingViewDiscovery:
    """A replaceable discovery source; it makes no direction decision."""

    name = "tradingview_cex"

    def __init__(self, web, *, pool_limit: int = 20):
        if not 10 <= pool_limit <= 50:
            raise ValueError("TradingView discovery pool must contain 10—50 rows")
        self.web = web
        self.pool_limit = pool_limit

    async def collect(self, tradable: set[str]) -> list[dict]:
        pages: list[dict] = []
        start = 0
        while True:
            body = {
                "filter": [
                    {"left": "exchange", "operation": "equal", "right": "BYBIT"},
                    {"left": "type", "operation": "equal", "right": "swap"},
                    {"left": "name", "operation": "match", "right": "USDT.P"},
                ],
                "columns": RANK_COLUMNS,
                "range": [start, start + 1000],
            }
            response = await self.web._post(
                SCREENER_URL,
                json=body,
                headers={"Accept": "application/json", "Referer": SCREENER_PAGE},
            )
            try:
                page = response.json()
            except ValueError as error:
                raise TradingViewError("TradingView CEX screener invalid JSON") from error
            if not isinstance(page, dict) or type(page.get("totalCount")) is not int or not isinstance(page.get("data"), list):
                raise TradingViewError("TradingView CEX screener invalid page")
            pages.append(page)
            start += len(page["data"])
            if start >= page["totalCount"]:
                break
            if not page["data"]:
                raise TradingViewError("TradingView CEX screener pagination stalled")
        # The existing validator checks every row, duplicates, pagination, identity and metrics.
        normalize_rankings(pages, "BTCUSDT", time.time())
        fetched = time.time()
        rows: list[dict] = []
        for page in pages:
            for raw in page["data"]:
                match = SYMBOL_RE.fullmatch(raw["s"])
                if match is None or match[1] not in tradable:
                    continue
                values = dict(zip(RANK_COLUMNS, raw["d"], strict=True))
                rows.append({
                    "symbol": match[1],
                    "source_symbol": raw["s"],
                    "metrics": {key: values[key] for key in RANK_METRICS},
                    "source_url": SCREENER_PAGE,
                    "collected_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
                })
        if not rows:
            raise TradingViewError("TradingView CEX screener has no confirmed tradable Bybit perpetual")
        for key in RANK_METRICS:
            available = [row for row in rows if row["metrics"][key] is not None]
            reverse = key != "24h_close_change|5"
            available.sort(key=lambda row: row["metrics"][key], reverse=reverse)
            for rank, row in enumerate(available, 1):
                row.setdefault("discovery_ranks", {})[key + (":desc" if reverse else ":asc")] = rank
            if key == "24h_close_change|5":
                available.reverse()
                for rank, row in enumerate(available, 1):
                    row.setdefault("discovery_ranks", {})[key + ":desc"] = rank
        # Bound expensive per-symbol page collection without the old Bybit market
        # quality gates. Keep liquid majors and both directional tails represented.
        def descending(row, metric):
            value = row["metrics"][metric]
            return -(value if value is not None else -float("inf"))

        buckets = {
            "volume": sorted(rows, key=lambda row: (
                descending(row, "24h_vol|5"), row["symbol"])),
            "gainers": sorted(rows, key=lambda row: (
                descending(row, "24h_close_change|5"), row["symbol"])),
            "decliners": sorted(rows, key=lambda row: (
                row["metrics"]["24h_close_change|5"] if row["metrics"]["24h_close_change|5"] is not None else float("inf"), row["symbol"])),
            "activity": sorted(rows, key=lambda row: (
                descending(row, "24h_vol_change|5"), row["symbol"])),
            "technical": sorted(rows, key=lambda row: (
                -abs(row["metrics"]["Recommend.All"] or 0), row["symbol"])),
        }
        selected: list[dict] = []
        seen: set[str] = set()

        def add(row, bucket):
            if row["symbol"] not in seen and len(selected) < self.pool_limit:
                row["discovery_bucket"] = bucket
                selected.append(row)
                seen.add(row["symbol"])

        for row in buckets["volume"][:max(8, self.pool_limit // 3)]:
            add(row, "volume")
        index = 0
        while len(selected) < min(len(rows), self.pool_limit):
            before = len(selected)
            for bucket in ("gainers", "decliners", "activity", "technical", "volume"):
                if index < len(buckets[bucket]):
                    add(buckets[bucket][index], bucket)
            if len(selected) == before and index >= len(rows):
                break
            index += 1
        for rank, row in enumerate(selected, 1):
            row["discovery_rank"] = rank
        for row in selected:
            row["market_rankings"] = normalize_rankings(pages, row["symbol"], fetched)
        return selected
