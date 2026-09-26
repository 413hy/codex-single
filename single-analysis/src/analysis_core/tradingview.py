"""Read the public requests used by TradingView's technicals webpage, without login.

This is an undocumented website dependency, not a supported public API. No retries,
authentication, proxy rotation or anti-bot bypass. Snapshots are NOT closed-bar signals.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

VERSION = "tradingview-web-v3"
# Match the website's available periods; 3m and 10m are deliberately not invented.
PERIODS = {"5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240", "1d": ""}
BASE = ("time", "open", "high", "low", "close", "volume")
RATINGS = ("Recommend.All", "Recommend.MA", "Recommend.Other")
OSCILLATORS = (
    "RSI", "RSI[1]", "Stoch.K", "Stoch.D", "Stoch.K[1]", "Stoch.D[1]",
    "CCI20", "CCI20[1]", "ADX", "ADX+DI", "ADX-DI", "AO", "AO[1]",
    "Mom", "Mom[1]", "MACD.macd", "MACD.signal", "Stoch.RSI.K", "W.R", "BBPower", "UO",
)
AVERAGES = tuple(f"{kind}{n}" for n in (10, 20, 30, 50, 100, 200) for kind in ("EMA", "SMA")) + (
    "Ichimoku.BLine", "VWMA", "HullMA9",
)
EXTRAS = ("ATR", "BB.upper", "BB.lower")
PIVOTS = tuple(
    f"Pivot.M.{method}.{level}"
    for method in ("Classic", "Fibonacci", "Camarilla", "Woodie", "Demark")
    for level in (("R1", "Middle", "S1") if method == "Demark" else (
        "R3", "R2", "R1", "Middle", "S1", "S2", "S3"
    ))
)
METRICS = BASE + RATINGS + OSCILLATORS + AVERAGES + EXTRAS + PIVOTS
IDENTITY = ("name", "exchange", "type", "typespecs", "update_mode")
# Exact-contract context exposed by the same public symbol request. A null value
# means unavailable, not zero; names/units remain the provider's own labels.
CONTEXT_METRICS = (
    "24h_close_change|5", "24h_vol|5", "24h_vol_change|5",
    "Perf.1M", "Perf.3M", "Perf.6M", "Perf.Y",
    "Volatility.D", "Volatility.W", "Volatility.M",
    "relative_volume_10d_calc", "OpenInterest", "funding_rate", "Perf.1W",
)
RANK_METRICS = ("24h_vol|5", "24h_vol_change|5", "24h_close_change|5", "Recommend.All")
RANK_COLUMNS = ("name", "exchange", "type", *RANK_METRICS)


class TradingViewError(ValueError):
    """Website data cannot safely be used for this analysis."""


def _card_text(article: str, qa_id: str) -> str | None:
    match = re.search(
        rf'<a\b[^>]*data-qa-id="{re.escape(qa_id)}"[^>]*>(.*?)</a>',
        article, re.I | re.S,
    )
    if not match:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", match[1]))).strip()


def extract_ideas(page_html: str, symbol: str, fetched: float) -> list[dict]:
    """Read visible idea cards; each card's own symbol label determines its scope."""
    target = f"BYBIT:{symbol}.P"
    match = re.search(r"window\.initData\.symbolInfo\s*=\s*", page_html)
    if not match:
        raise TradingViewError("TradingView ideas page identity missing")
    try:
        info, _ = json.JSONDecoder().raw_decode(page_html[match.end():])
    except (ValueError, TypeError) as error:
        raise TradingViewError("TradingView ideas page identity invalid") from error
    if not isinstance(info, dict) or info.get("resolved_symbol") != target:
        raise TradingViewError("TradingView ideas page resolved to a different instrument")
    ideas = []
    for article in re.findall(r"<article\b.*?</article>", page_html, re.I | re.S):
        title = _card_text(article, "ui-lib-card-link-title")
        if not title:
            continue
        anchor = re.search(r'<a\b[^>]*data-qa-id="ui-lib-card-link-title"[^>]*>', article, re.I | re.S)
        link = re.search(r'href="([^"]+)"', anchor[0]) if anchor else None
        url = html.unescape(link[1]) if link else ""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.tradingview.com" or not parsed.path.startswith("/chart/"):
            continue
        icon = re.search(r'<a\b[^>]*data-qa-id="ui-lib-card-preview-link-icon"[^>]*>', article, re.I | re.S)
        origin = re.search(r'title="([A-Z0-9]+:[A-Z0-9.]+)"', icon[0]) if icon else None
        if not origin or origin[1].split(":", 1)[1].removesuffix(".P") != symbol:
            continue
        author_tag = re.search(r'<a\b[^>]*href="(/u/[^"/]+/)"[^>]*>', article, re.I | re.S)
        author = html.unescape(author_tag[1].split("/")[2]) if author_tag else None
        time_tag = re.search(r'<time\b[^>]*>', article, re.I | re.S)
        card_time = re.search(r'dateTime="([^"]+)"', time_tag[0], re.I) if time_tag else None
        timestamp = None
        if card_time:
            try:
                timestamp = datetime.fromisoformat(card_time[1].replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
        direction_tag = re.search(r'<span\b[^>]*title="(Long|Short)"[^>]*>', article, re.I | re.S)
        ideas.append({
            "title": title,
            "excerpt": _card_text(article, "ui-lib-card-link-paragraph"),
            "source_symbol": origin[1],
            "same_bybit_perpetual": origin[1] == target,
            "author": author,
            "idea_url": url,
            "card_time": timestamp,
            "card_time_semantics": "published_or_updated; page does not distinguish reliably",
            "strategy_label": direction_tag[1].upper() if direction_tag else None,
            "fetched_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
            "role": "user_opinion_weak_context_not_market_fact",
        })
    return ideas


def normalize_rankings(
    pages: list[dict], symbol: str, fetched: float, *, columns: tuple[str, ...] = RANK_COLUMNS,
) -> dict:
    """Rank only exact BYBIT USDT perpetual rows within the reported scan scope."""
    rows: dict[str, dict] = {}
    total = None
    for page in pages:
        if type(page.get("totalCount")) is not int or page["totalCount"] < 0 or not isinstance(page.get("data"), list):
            raise TradingViewError("TradingView screener response shape changed")
        if total is None:
            total = page["totalCount"]
        elif total != page["totalCount"]:
            raise TradingViewError("TradingView screener scope changed across pages")
        for row in page["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("d"), list) or len(row["d"]) != len(columns):
                raise TradingViewError("TradingView screener row shape changed")
            item = dict(zip(columns, row["d"], strict=True))
            key = row.get("s")
            if (not isinstance(key, str) or not re.fullmatch(r"BYBIT:[A-Z0-9]{1,24}USDT\.P", key)
                    or item["name"] != key.split(":", 1)[1]
                    or item["exchange"] != "BYBIT" or item["type"] != "swap"):
                raise TradingViewError("TradingView screener instrument identity mismatch")
            if key in rows:
                raise TradingViewError("TradingView screener duplicate instrument")
            for metric in RANK_METRICS:
                value = item[metric]
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                    raise TradingViewError("TradingView screener invalid metric: " + metric)
            rows[key] = item
    if total is None or len(rows) != total:
        raise TradingViewError("TradingView screener incomplete result")
    target = rows.get(f"BYBIT:{symbol}.P")
    metrics = {}
    if target:
        for metric in RANK_METRICS:
            value = target[metric]
            if value is None:
                continue
            population = [row[metric] for row in rows.values() if row[metric] is not None]
            metrics[metric] = {
                "value": value, "rank_desc": 1 + sum(other > value for other in population),
                "rank_asc": 1 + sum(other < value for other in population),
                "available_count": len(population),
            }
    return {
        "scope": "TradingView crypto screener: BYBIT swap name matching USDT.P",
        "source_symbol": f"BYBIT:{symbol}.P", "scope_count": total,
        "target_present": target is not None, "metrics": metrics,
        "fetched_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
        "role": "same_market_relative_rank_weak_context_not_selection",
    }


def field(name: str, interval: str) -> str:
    return name + ("|" + interval if interval else "")


def _valid_extra_value(value: Any) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_valid_extra_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _valid_extra_value(item) for key, item in value.items())
    return False


def normalize(raw: dict, symbol: str, fetched: float, extra_fields: tuple[str, ...] = ()) -> dict:
    if (raw.get("name") != symbol + ".P" or raw.get("exchange") != "BYBIT"
            or raw.get("type") != "swap" or not isinstance(raw.get("typespecs"), list)
            or "perpetual" not in raw["typespecs"]):
        raise TradingViewError("TradingView symbol/exchange/perpetual identity mismatch")
    context_metrics, unavailable_context = {}, {}
    for name in CONTEXT_METRICS:
        value = raw.get(name)
        if value is None:
            unavailable_context[name] = "provider_null" if name in raw else "not_returned"
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise TradingViewError(f"TradingView invalid numeric value: context/{name}")
        else:
            context_metrics[name] = value
    extra_values, unavailable_extra = {}, {}
    for name in extra_fields:
        value = raw.get(name)
        if value is None:
            unavailable_extra[name] = "provider_null" if name in raw else "not_returned"
        elif not _valid_extra_value(value):
            raise TradingViewError(f"TradingView invalid extra field: {name}")
        else:
            extra_values[name] = value
    periods = {}
    for tf, interval in PERIODS.items():
        values, missing = {}, []
        for name in METRICS:
            value = raw.get(field(name, interval))
            if value is None:
                missing.append(name)
            elif type(value) not in (int, float) or not math.isfinite(value):
                raise TradingViewError(f"TradingView invalid numeric value: {tf}/{name}")
            else:
                values[name] = value
        duration = int(interval or 1440) * 60
        opened = values.get("time")
        if opened is not None and (opened % duration or opened > fetched + 5):
            raise TradingViewError(f"TradingView future/misaligned period: {tf}")
        prices = {name: values[name] for name in ("open", "high", "low", "close") if name in values}
        if any(value <= 0 for value in prices.values()) or values.get("volume", 0) < 0:
            raise TradingViewError(f"TradingView invalid OHLCV: {tf}")
        ohlcv_complete = all(name in values for name in BASE)
        if ohlcv_complete and (
            values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
            or values["low"] > values["high"]
        ):
            raise TradingViewError(f"TradingView invalid OHLCV: {tf}")
        if any(not -1 <= values[k] <= 1 for k in RATINGS if k in values):
            raise TradingViewError(f"TradingView rating outside [-1,1]: {tf}")
        periods[tf] = {
            "bar_open_at": datetime.fromtimestamp(opened, UTC).isoformat() if opened is not None else None,
            "ohlcv_complete": ohlcv_complete,
            "closed_bar_confirmed": False,
            "values": values,
            "unavailable_fields": missing,
        }
    return {
        "version": VERSION, "source": "TRADINGVIEW_PUBLIC_WEB", "symbol": symbol,
        "source_symbol": f"BYBIT:{symbol}.P", "instrument": "USDT perpetual",
        "provider_update_mode": raw.get("update_mode"),
        "fetched_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
        "source_quote_timestamp": None, "quote_freshness_verified": False,
        "periods": periods,
        "context_metrics": {
            "values": context_metrics,
            "unavailable_fields": unavailable_context,
            "source_symbol": f"BYBIT:{symbol}.P",
            "unit_note": "TradingView原字段名与原值；字段单位和|5统计口径未独立核实。",
            "quote_freshness_verified": False,
        },
        "extra_fields": {
            "values": extra_values,
            "unavailable_fields": unavailable_extra,
            "source_symbol": f"BYBIT:{symbol}.P",
            "role": "website_field_untrusted_reference",
        },
        "limitations": [
            "Technical snapshots may be cached; not confirmed closed-bar values; can repaint.",
            "time identifies bar opening, not last quote/update time; exact quote age unknown.",
            "TradingView and Bybit may share underlying data; not independent confirmation.",
            "Ratings are correlated technical summaries, not probabilities or directions.",
            "Pivot.M.* are provider pivot labels, not measured volume-profile support.",
            "No 3m/10m, historical indicator series, private Pine, news or footprint in this source.",
            "15m/30m/1h support the next 1-2h question; 2h, 4h, 1d and available weekly structure are background.",
            "Context metrics are same-contract website snapshots; absent/null is not zero.",
        ],
    }


def require_recent_core(result: dict) -> None:
    """A short-horizon direction needs populated, recent TradingView core bars."""
    fetched = datetime.fromisoformat(result["fetched_at"])
    for timeframe, minutes in (("15m", 15), ("30m", 30), ("1h", 60)):
        period = result["periods"][timeframe]
        opened = period["bar_open_at"]
        if not period["ohlcv_complete"] or opened is None:
            raise TradingViewError("TradingView core direction fields unavailable: " + timeframe)
        age = (fetched - datetime.fromisoformat(opened)).total_seconds()
        if not -5 <= age <= minutes * 120:
            raise TradingViewError("TradingView core direction bar is stale: " + timeframe)


class TradingViewWeb:
    def __init__(self, client: httpx.AsyncClient | None = None, *, extra_fields: str = ""):
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        requested = tuple(dict.fromkeys(x.strip() for x in extra_fields.split(",") if x.strip()))
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.|]{0,127}", x) for x in requested):
            raise ValueError("Invalid TradingView extra field name")
        self.extra_fields = tuple(x for x in requested if x not in CONTEXT_METRICS and x not in IDENTITY)
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def _get(self, url, *, allow_404=False, **kwargs):
        async with self._lock:
            await asyncio.sleep(max(0, 1 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            response = await self.client.get(url, **kwargs)
        if response.status_code != 200 and not (allow_404 and response.status_code == 404):
            raise TradingViewError(f"TradingView website HTTP {response.status_code}; no retry")
        if len(response.content) > 2_000_000:
            raise TradingViewError("TradingView oversized response")
        return response

    async def _post(self, url, **kwargs):
        async with self._lock:
            await asyncio.sleep(max(0, 1 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            response = await self.client.post(url, **kwargs)
        if response.status_code != 200 or len(response.content) > 2_000_000:
            raise TradingViewError(f"TradingView screener HTTP {response.status_code} or oversized response")
        return response

    async def _rankings(self, symbol: str) -> tuple[dict, list[dict]]:
        pages, requests = [], []
        start = 0
        while True:
            body = {
                "filter": [
                    {"left": "exchange", "operation": "equal", "right": "BYBIT"},
                    {"left": "type", "operation": "equal", "right": "swap"},
                    {"left": "name", "operation": "match", "right": "USDT.P"},
                ],
                "columns": RANK_COLUMNS, "range": [start, start + 1000],
            }
            response = await self._post(
                "https://scanner.tradingview.com/crypto/scan", json=body,
                headers={"Accept": "application/json", "Referer": "https://www.tradingview.com/crypto-screener/"},
            )
            try:
                page = response.json()
            except ValueError as error:
                raise TradingViewError("TradingView screener invalid JSON") from error
            if not isinstance(page, dict):
                raise TradingViewError("TradingView screener invalid shape")
            pages.append(page)
            requests.append({
                "request_url": str(response.request.url), "request_body": body,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "http_date": response.headers.get("date"), "http_age": response.headers.get("age"),
            })
            total = page.get("totalCount")
            if type(total) is not int or not isinstance(page.get("data"), list):
                raise TradingViewError("TradingView screener invalid page")
            start += len(page["data"])
            if start >= total:
                break
            if not page["data"]:
                raise TradingViewError("TradingView screener pagination stalled")
        return normalize_rankings(pages, symbol, time.time()), requests

    async def collect(self, symbol: str, *, market_rankings: dict | None = None) -> dict:
        return await self._collect(symbol, market_rankings=market_rankings)

    async def _collect(self, symbol: str, *, market_rankings: dict | None = None) -> dict:
        if not re.fullmatch(r"[A-Z0-9]{1,24}USDT", symbol):
            raise TradingViewError("Invalid TradingView symbol")
        page_url = f"https://www.tradingview.com/symbols/{symbol}.P/technicals/?exchange=BYBIT"
        page = await self._get(page_url, allow_404=True)
        technical_page_available = page.status_code == 200
        verified_page_url = page_url
        if not technical_page_available:
            verified_page_url = f"https://www.tradingview.com/symbols/{symbol}.P/?exchange=BYBIT"
            page = await self._get(verified_page_url)
        match = re.search(r"window\.initData\.symbolInfo\s*=\s*", page.text)
        if not match:
            raise TradingViewError("TradingView page identity missing; blocked or changed HTML")
        try:
            info, _ = json.JSONDecoder().raw_decode(page.text[match.end():])
        except (ValueError, TypeError) as error:
            raise TradingViewError("TradingView page identity invalid") from error
        if not isinstance(info, dict) or info.get("resolved_symbol") != f"BYBIT:{symbol}.P":
            raise TradingViewError("TradingView page resolved to a different instrument")
        raw: dict[str, Any] = {}
        requests = []
        for index, interval in enumerate(PERIODS.values()):
            fields = list(IDENTITY) + [field(n, interval) for n in METRICS]
            if index == 0:
                fields += CONTEXT_METRICS
            response = await self._get(
                "https://scanner.tradingview.com/symbol",
                params={"symbol": f"BYBIT:{symbol}.P", "fields": ",".join(fields)},
                headers={"Accept": "application/json", "Referer": page_url},
            )
            try:
                part = response.json()
            except ValueError as error:
                raise TradingViewError("TradingView invalid JSON") from error
            if not isinstance(part, dict):
                raise TradingViewError("TradingView invalid response shape")
            if any(key in raw and raw[key] != part.get(key) for key in IDENTITY):
                raise TradingViewError("TradingView changed identity between periods")
            raw.update(part)
            requests.append({
                "request_url": str(response.request.url),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "http_date": response.headers.get("date"),
                "http_age": response.headers.get("age"),
            })
        # The website only returns explicitly requested fields. Accept additional
        # verified field names without changing the collector or direction schema.
        pending = list(self.extra_fields)
        while pending:
            chunk = []
            size = 0
            while pending and size + len(pending[0]) + 1 <= 4000:
                item = pending.pop(0)
                chunk.append(item)
                size += len(item) + 1
            if not chunk:
                chunk.append(pending.pop(0))
            response = await self._get(
                "https://scanner.tradingview.com/symbol",
                params={"symbol": f"BYBIT:{symbol}.P", "fields": ",".join((*IDENTITY, *chunk))},
                headers={"Accept": "application/json", "Referer": page_url},
            )
            try:
                part = response.json()
            except ValueError as error:
                raise TradingViewError("TradingView invalid JSON") from error
            if not isinstance(part, dict) or any(part.get(key) != raw.get(key) for key in IDENTITY):
                raise TradingViewError("TradingView extra fields identity mismatch")
            raw.update(part)
            requests.append({
                "request_url": str(response.request.url),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "http_date": response.headers.get("date"),
                "http_age": response.headers.get("age"),
            })
        result = normalize(raw, symbol, time.time(), self.extra_fields)
        require_recent_core(result)
        result["provenance"] = {
            "page_url": verified_page_url, "page_sha256": hashlib.sha256(page.content).hexdigest(),
            "technical_page_unavailable": None if technical_page_available else "HTTP_404",
            "requests": requests, "raw_fields": raw,
        }
        result["source_provenance"] = {
            "technical_page_url": page_url if technical_page_available else None,
            "technical_page_sha256": hashlib.sha256(page.content).hexdigest() if technical_page_available else None,
            "verified_symbol_page_url": verified_page_url,
            "technical_page_unavailable": None if technical_page_available else "HTTP_404",
            "scanner_symbol_url": "https://scanner.tradingview.com/symbol",
            "scanner_symbol_response_sha256": [item["response_sha256"] for item in requests],
            "verified_source_symbol": f"BYBIT:{symbol}.P",
        }
        ideas_url = f"https://www.tradingview.com/symbols/{symbol}.P/ideas/?exchange=BYBIT"
        try:
            ideas_page = await self._get(ideas_url)
            result["community_ideas"] = extract_ideas(ideas_page.text, symbol, time.time())
            result["provenance"]["ideas_page"] = {
                "url": ideas_url, "sha256": hashlib.sha256(ideas_page.content).hexdigest(),
                "http_date": ideas_page.headers.get("date"),
                "http_age": ideas_page.headers.get("age"),
            }
            result["source_provenance"]["ideas_page_url"] = ideas_url
            result["source_provenance"]["ideas_page_sha256"] = hashlib.sha256(ideas_page.content).hexdigest()
        except (TradingViewError, httpx.HTTPError, TimeoutError) as error:
            result["community_ideas"] = []
            result["community_ideas_unavailable"] = type(error).__name__
        try:
            if market_rankings is None:
                result["market_rankings"], result["provenance"]["ranking_requests"] = await self._rankings(symbol)
            elif market_rankings.get("source_symbol") != f"BYBIT:{symbol}.P" or not market_rankings.get("target_present"):
                raise TradingViewError("TradingView discovery ranking identity mismatch")
            else:
                result["market_rankings"] = market_rankings
                result["provenance"]["ranking_requests"] = []
            result["source_provenance"]["ranking_page_url"] = "https://www.tradingview.com/crypto-screener/"
            result["source_provenance"]["ranking_response_sha256"] = [
                item["response_sha256"] for item in result["provenance"]["ranking_requests"]
            ]
        except (TradingViewError, httpx.HTTPError, TimeoutError) as error:
            result["market_rankings_unavailable"] = type(error).__name__
        if not (
            result["context_metrics"]["values"]
            or result["extra_fields"]["values"]
            or any(period["values"] for period in result["periods"].values())
            or result["community_ideas"]
        ):
            raise TradingViewError("TradingView identity verified but no usable reference data")
        return result

    async def close(self):
        await self.client.aclose()
