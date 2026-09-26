"""Browser-owned scanner responses with centralized DOM fallbacks."""

from common.network import browser_proxy
import asyncio
import json
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from common.config import settings
from common.db import source_health, get_setting
from signals.sources import item

VERSION = "tv-2-selective"
SELECTORS = {
    "rows": 'tbody tr, [role="row"]',
    "headers": 'thead th, [role="columnheader"]',
    "cells": 'td, [role="cell"], [role="gridcell"]',
    "ideas": 'article, [class*="idea-card"], [class*="card-exterior"]',
    "idea_author": 'a[href*="/u/"]',
    "idea_title": '[data-qa-id="ui-lib-card-link-title"]',
    "idea_paragraph": '[data-qa-id="ui-lib-card-link-paragraph"]',
    "idea_direction": '[title="Long"], [title="Short"]',
    "idea_link": 'a[href*="/chart/"]',
}
PAGES = {
    "coins": "https://www.tradingview.com/crypto-coins-screener/",
    "cex": "https://www.tradingview.com/crypto-screener/",
}
RANKINGS = {
    "coins": [
        ("24h_gainers", "24h_close_change|5", "desc"),
        ("24h_losers", "24h_close_change|5", "asc"),
        ("volume_usd_24h", "24h_vol_cmc", "desc"),
        ("volume_change_24h", "24h_vol_change_cmc", "desc"),
        ("technical_rating_bullish", "Recommend.All", "desc"),
        ("technical_rating_bearish", "Recommend.All", "asc"),
    ],
    "cex": [
        ("24h_gainers", "24h_close_change|5", "desc"),
        ("24h_losers", "24h_close_change|5", "asc"),
        ("volume_usd_24h", "24h_vol|5", "desc"),
        ("volume_change_24h", "24h_vol_change|5", "desc"),
        ("technical_rating_bullish", "Recommend.All", "desc"),
        ("technical_rating_bearish", "Recommend.All", "asc"),
    ],
}


async def collect(universe, top_n):
    limits = get_setting("ranking_limits") or {}
    market_type = getattr(universe, "market_type", "futures")
    suffix = "" if market_type == "spot" else ".P"
    technical_fields = ["Recommend.All|5", "Recommend.All|15", "Recommend.All|60", "Recommend.All|240"]
    out = []
    settings.tradingview_profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(settings.tradingview_profile_dir),
            headless=True,
            proxy=browser_proxy(settings.network_proxy),
            args=["--no-sandbox"],
            locale="en-US",
        )
        try:
            for name, url in PAGES.items():
                page = await browser.new_page()
                captures = []

                async def capture(response):
                    if "/scan" in response.url and "scanner.tradingview.com" in response.url:
                        try:
                            data = await response.json()
                            body = response.request.post_data_json
                            if isinstance(body, dict) and isinstance(data, dict) and "data" in data:
                                captures.append((response.url, body, data))
                        except Exception:
                            return

                page.on("response", capture)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    for _ in range(20):
                        if captures:
                            break
                        await asyncio.sleep(0.5)
                    text = await page.inner_text("body")
                    if any(
                        s in text.lower() for s in ("verify you are human", "just a moment", "access denied")
                    ):
                        raise RuntimeError("TradingView human verification required")
                    results = []
                    if captures:
                        endpoint, body, _ = captures[-1]
                        for kind, field, order in RANKINGS[name]:
                            limit = limits.get(name + "_" + kind, top_n)
                            request = json.loads(json.dumps(body))
                            columns = []  # Explicit whitelist, never inherit every visible scanner column.
                            for col in ("name", "base_currency", field, *technical_fields):
                                if col not in columns:
                                    columns.append(col)
                            request["columns"] = columns
                            request["sort"] = {"sortBy": field, "sortOrder": order}
                            request["range"] = [0, limit]
                            if name == "cex":
                                request["symbols"] = {
                                    "tickers": ["BINANCE:" + s + suffix for s in universe.rows]
                                }
                            else:
                                request["symbols"] = {
                                    "tickers": [
                                        "CRYPTO:" + r["baseAsset"] + "USD" for r in universe.rows.values()
                                    ]
                                }
                            if name == "cex":
                                request["filter"] = [
                                    {"left": "exchange", "operation": "equal", "right": "BINANCE"},
                                    {"left": "name", "operation": "match", "right": "USDT" + suffix},
                                ]
                            response = await browser.request.post(endpoint, data=request, timeout=20000)
                            if not response.ok:
                                # Optional timeframe fields may differ by scanner. Retry minimal data.
                                columns = ["name", "base_currency", field]
                                request["columns"] = list(dict.fromkeys(columns))
                                columns = request["columns"]
                                response = await browser.request.post(endpoint, data=request, timeout=20000)
                            if not response.ok:
                                source_health(
                                    "tradingview_" + name + "_" + kind,
                                    "HTTP_" + str(response.status),
                                    collector_version=VERSION,
                                )
                                continue
                            doc = await response.json()
                            rank = 0
                            for row in doc.get("data", []):
                                mapped = dict(zip(columns, row.get("d", [])))
                                symbol = universe.map(row.get("s", "")) or universe.map(
                                    str(mapped.get("base_currency", ""))
                                )
                                if not symbol or mapped.get(field) is None:
                                    continue
                                if name == "cex" and (
                                    not row["s"].startswith("BINANCE:")
                                    or not row["s"].endswith("USDT" + suffix)
                                ):
                                    continue
                                rank += 1
                                results.append(
                                    item(
                                        "tradingview",
                                        symbol,
                                        f"{name} {kind} #{rank}: {mapped.get(field)}",
                                        rank=rank,
                                        ranking_type=name + "_" + kind,
                                        url="",
                                        raw={
                                            "row": row,
                                            "fields": mapped,
                                            "page": url,
                                            "collector_version": VERSION,
                                            "transport": "browser_json",
                                            "market_type": market_type,
                                            "missing_fields": [
                                                f for f in technical_fields if mapped.get(f) is None
                                            ],
                                            "rating_role": "correlated_discovery_evidence",
                                        },
                                    )
                                )
                                # Ranking strips raw_payload before the model: expose only useful,
                                # explicitly requested fields in its compact evidence packet.
                                results[-1]["metrics"] = {
                                    "market_type": market_type,
                                    "ranking_metric": {field: mapped.get(field)},
                                    "technical_ratings": {
                                        tf: mapped.get(key)
                                        for tf, key in zip(("5m", "15m", "1h", "4h"), technical_fields)
                                    },
                                    "rating_role": "correlated_discovery_evidence",
                                }
                                if rank >= limit:
                                    break
                            source_health(
                                "tradingview_" + name + "_" + kind, count=rank, collector_version=VERSION
                            )
                    if not results:
                        results = await dom_rankings(page, name, universe, top_n, url)
                    if not results:
                        raise RuntimeError("TradingView screener selectors/response unavailable")
                    out.extend(results)
                    source_health("tradingview_" + name, count=len(results), collector_version=VERSION)
                except Exception as exc:
                    source_health("tradingview_" + name, str(exc)[:200], collector_version=VERSION)
                finally:
                    await page.close()
            for mode, suffix in [("most_recent", "?sort=recent"), ("most_popular", "")]:
                page = await browser.new_page()
                try:
                    url = "https://www.tradingview.com/markets/cryptocurrencies/ideas/" + suffix
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    soup = BeautifulSoup(await page.content(), "html.parser")
                    cards = soup.select(SELECTORS["ideas"])
                    found = 0
                    for rank, card in enumerate(
                        cards[: limits.get(mode, 50 if mode == "most_recent" else 20)], 1
                    ):
                        a = card.select_one(SELECTORS["idea_link"])
                        if not a:
                            continue
                        text = card.get_text(" ", strip=True)
                        author = card.select_one(SELECTORS["idea_author"])
                        title = card.select_one(SELECTORS["idea_title"])
                        direction = card.select_one(SELECTORS["idea_direction"])
                        paragraph = card.select_one(SELECTORS["idea_paragraph"])
                        if paragraph:
                            text = (
                                title.get_text(" ", strip=True) + "\n" if title else ""
                            ) + paragraph.get_text(" ", strip=True)
                        symbols = universe.extract(text)
                        symbols += universe.extract(a.get("href", "").upper())
                        if not symbols:
                            continue
                        date = card.select_one("time")
                        at = None
                        if date and date.get("datetime"):
                            from datetime import datetime

                            at = datetime.fromisoformat(date["datetime"].replace("Z", "+00:00")).timestamp()
                        for symbol in set(symbols):
                            # Missing publish date is explicit, never claim collection time is publication time.
                            row = item(
                                "tradingview",
                                symbol,
                                text,
                                at=at or time.time() - 86400,
                                rank=rank,
                                ranking_type=mode,
                                url=urljoin(url, a["href"]),
                                raw={
                                    "published_at_unknown": at is None,
                                    "market_type": market_type,
                                    "timeframe": None,
                                    "collector_version": VERSION,
                                    "direction_tag": "UNKNOWN",
                                    "html": str(card),
                                },
                            )
                            if at is None:
                                row["published_at"] = None
                            row["source_type"] = "article"
                            row.update(
                                author=author.get_text(" ", strip=True).removeprefix("by ")
                                if author
                                else None,
                                title=title.get_text(" ", strip=True) if title else None,
                                direction_tag=direction["title"].upper() if direction else "UNKNOWN",
                            )
                            out.append(row)
                            found += 1
                    if not cards:
                        raise RuntimeError("TradingView Ideas DOM unavailable")
                    source_health("tradingview_ideas_" + mode, count=found, collector_version=VERSION)
                except Exception as exc:
                    source_health("tradingview_ideas_" + mode, str(exc)[:200], collector_version=VERSION)
                finally:
                    await page.close()
        finally:
            await browser.close()
    if not out:
        raise RuntimeError("All TradingView sections unavailable; inspect source health")
    return out


async def dom_rankings(page, name, universe, top_n, url):
    """Read by header labels. Only rank visible rows, clearly label partial coverage."""
    market_type = getattr(universe, "market_type", "futures")
    headers = await page.locator(SELECTORS["headers"]).all_inner_texts()
    out = []
    for rank, row in enumerate(await page.locator(SELECTORS["rows"]).all(), 1):
        cells = await row.locator(SELECTORS["cells"]).all_inner_texts()
        if len(cells) != len(headers):
            continue
        text = " ".join(cells)
        symbols = universe.extract(text)
        if name == "cex" and (
            "BINANCE" not in text.upper()
            or (market_type == "futures" and "USDT.P" not in text.upper() and "PERPETUAL" not in text.upper())
            or (
                market_type == "spot"
                and (".P" in text.upper() or "PERPETUAL" in text.upper() or "SPOT" not in text.upper())
            )
        ):
            continue
        for symbol in symbols[:1]:
            out.append(
                item(
                    "tradingview",
                    symbol,
                    text,
                    rank=rank,
                    ranking_type=name + "_visible_table",
                    raw={
                        "fields": dict(zip(headers, cells)),
                        "page": url,
                        "collector_version": VERSION,
                        "partial_coverage": True,
                        "transport": "dom",
                    },
                )
            )
        if len(out) >= top_n:
            break
    return out
