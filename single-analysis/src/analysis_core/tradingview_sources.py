"""Optional public TradingView page adapters for asset-level background evidence."""

from __future__ import annotations

import asyncio
import html
import math
import os
import re
import shutil
import signal
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

from analysis_core.tradingview import TradingViewError

COIN_COLUMNS = (
    "name", "24h_vol_cmc", "24h_close_change|5", "market_cap_basic",
    "social_volume", "social_dominance", "sentiment", "Perf.1W", "Volatility.W",
)


class TradingViewSource(Protocol):
    name: str
    stage: str

    async def collect(self, symbol: str) -> dict | list[dict]: ...


class CoinContextSource:
    """Crypto Coins Screener is aggregated by coin, not a Bybit contract quote."""

    name = "coin_context"
    stage = "initial"

    def __init__(self, web):
        self.web = web

    async def collect(self, symbol: str) -> dict:
        coin = symbol.removesuffix("USDT") + "USD"
        body = {
            "filter": [{"left": "name", "operation": "equal", "right": coin}],
            "columns": COIN_COLUMNS,
            "range": [0, 2],
        }
        response = await self.web._post(
            "https://scanner.tradingview.com/coin/scan", json=body,
            headers={"Accept": "application/json", "Referer": "https://www.tradingview.com/crypto-coins-screener/"},
        )
        try:
            result = response.json()
        except ValueError as error:
            raise TradingViewError("TradingView coin screener invalid JSON") from error
        if not isinstance(result, dict) or result.get("totalCount") != 1 or len(result.get("data", [])) != 1:
            raise TradingViewError("TradingView coin background unavailable or ambiguous")
        row = result["data"][0]
        if row.get("s") != "CRYPTO:" + coin or not isinstance(row.get("d"), list) or len(row["d"]) != len(COIN_COLUMNS):
            raise TradingViewError("TradingView coin identity mismatch")
        values = dict(zip(COIN_COLUMNS, row["d"], strict=True))
        if values["name"] != coin or any(
            value is not None and (type(value) not in (int, float) or not math.isfinite(value))
            for name, value in values.items() if name != "name"
        ):
            raise TradingViewError("TradingView coin context invalid field")
        return {
            "source_symbol": row["s"], "scope": "aggregated_coin_not_bybit_contract",
            "source_url": "https://www.tradingview.com/crypto-coins-screener/",
            "collected_at": datetime.now(UTC).isoformat(),
            "values": {k: v for k, v in values.items() if k != "name" and v is not None},
            "unavailable_fields": [k for k, v in values.items() if k != "name" and v is None],
        }


class NewsPageSource:
    """Render the public symbol news page; headlines are asset-level event background."""

    name = "news"
    stage = "final"

    def __init__(self, chromium: str | None = None):
        self.chromium = chromium or shutil.which("chromium")

    async def collect(self, symbol: str) -> list[dict]:
        if self.chromium is None:
            raise TradingViewError("Chromium unavailable for public TradingView news page")
        url = f"https://www.tradingview.com/symbols/{symbol}.P/news/?exchange=BYBIT"
        proc = await asyncio.create_subprocess_exec(
            self.chromium, "--headless", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--virtual-time-budget=7000", "--dump-dom", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (TimeoutError, asyncio.CancelledError) as error:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise TradingViewError("TradingView news page render timed out") from None
        if proc.returncode or len(output) > 3_000_000:
            raise TradingViewError("TradingView news page render failed")
        page = output.decode(errors="replace")
        marker = re.search(r"window\.initData\.symbolInfo\s*=\s*", page)
        if not marker or f'"resolved_symbol":"BYBIT:{symbol}.P"' not in page[marker.end():marker.end() + 1000]:
            raise TradingViewError("TradingView news page contract identity mismatch")
        news = []
        for match in re.finditer(
            r'<a href="(/news/[^\"]+)"[^>]*>\s*<article\b[^>]*data-qa-id="news-headline-card".*?</article></a>',
            page, re.I | re.S,
        ):
            fragment = match[0]
            title_match = re.search(
                r'<[^>]*data-qa-id="news-headline-title"[^>]*>(.*?)</[^>]+>',
                fragment, re.I | re.S,
            )
            if not title_match:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", " ", title_match[1])).strip()
            clock = re.search(r'<relative-time\b[^>]*event-time="([^"]+)"', fragment)
            provider = re.search(r'<span\b[^>]*class="provider-[^"]*"[^>]*>\s*<span[^>]*>(.*?)</span>', fragment, re.I | re.S)
            if not title or not clock:
                continue
            try:
                published = parsedate_to_datetime(html.unescape(clock[1])).astimezone(UTC).isoformat()
            except (TypeError, ValueError):
                continue
            news.append({
                "title": title[:500],
                "url": "https://www.tradingview.com" + html.unescape(match[1]),
                "provider": html.unescape(re.sub(r"<[^>]+>", " ", provider[1])).strip() if provider else None,
                "published_at": published,
                "source_symbol_page": f"BYBIT:{symbol}.P",
                "scope": "asset_news_background_not_contract_quote",
            })
            if len(news) >= 10:
                break
        if not news:
            raise TradingViewError("TradingView news page has no verified visible headlines")
        return news


class SourceRegistry:
    """Adding a public source only requires another adapter with name/stage/collect."""

    def __init__(self, sources: tuple[TradingViewSource, ...]):
        names = [source.name for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate TradingView source names")
        self.sources = sources

    async def enrich(self, symbol: str, evidence: dict, *, stage: str) -> dict:
        result = dict(evidence)
        for source in self.sources:
            if source.stage == "final" and stage != "final":
                continue
            if source.name in result or source.name + "_unavailable" in result:
                continue
            try:
                result[source.name] = await source.collect(symbol)
            except (TradingViewError, OSError, TimeoutError) as error:
                result[source.name + "_unavailable"] = type(error).__name__ + ": " + str(error)[:160]
        return result
