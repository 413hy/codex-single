"""Two model stages: TradingView discovery and Bybit-confirmed final directions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analysis_core.config import (
    DIRECTION_MODEL,
    DIRECTION_REASONING,
    TV_SELECTION_MODEL,
    TV_SELECTION_REASONING,
)
from analysis_core.model import Decision, ModelProfile, ModelService
from analysis_core.store import identity


class TVCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}USDT$")
    rank: int = Field(ge=1, le=10)
    direction: Literal["LONG", "SHORT"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reason: str = Field(min_length=4, max_length=600)


class TVSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidates: list[TVCandidate] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def ranks(self):
        if sorted(item.rank for item in self.candidates) != list(range(1, len(self.candidates) + 1)):
            raise ValueError("TradingView candidate ranks must be contiguous")
        if len({item.symbol for item in self.candidates}) != len(self.candidates):
            raise ValueError("TradingView candidate symbols must be unique")
        return self


class FinalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str = Field(pattern=r"^[A-Z0-9]{1,24}USDT$")
    rank: int = Field(ge=1, le=3)
    direction: Literal["LONG", "SHORT"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reason: str = Field(min_length=4, max_length=600)


class FinalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selected: list[FinalCandidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def ranks(self):
        if sorted(item.rank for item in self.selected) != list(range(1, len(self.selected) + 1)):
            raise ValueError("Final candidate ranks must be contiguous")
        if len({item.symbol for item in self.selected}) != len(self.selected):
            raise ValueError("Final candidate symbols must be unique")
        return self


TV_SELECTION_PROFILE = ModelProfile(
    "tv_discovery", TV_SELECTION_MODEL, TV_SELECTION_REASONING,
    frozenset({"tv_selection_v1.md"}), "TV_SELECTION_MODEL",
)
FINAL_SELECTION_PROFILE = ModelProfile(
    "direction_selection", DIRECTION_MODEL, DIRECTION_REASONING,
    frozenset({"direction_selection_v1.md"}), "FINAL_SELECTION_MODEL",
)


def strict_schema(value):
    if isinstance(value, dict):
        value.pop("default", None)
        if isinstance(value.get("properties"), dict):
            value["required"] = list(value["properties"])
            value["additionalProperties"] = False
        for child in value.values():
            strict_schema(child)
    elif isinstance(value, list):
        for child in value:
            strict_schema(child)


def compact_tradingview(data):
    """Keep source facts useful to 1—2h reasoning; the raw snapshot stays in events."""
    fields = {
        "time", "open", "high", "low", "close", "volume", "Recommend.All",
        "Recommend.MA", "Recommend.Other", "RSI", "RSI[1]", "ADX", "ADX+DI",
        "ADX-DI", "MACD.macd", "MACD.signal", "EMA20", "EMA50", "SMA50",
        "ATR", "BB.upper", "BB.lower", "VWMA",
    }
    periods = {}
    for timeframe, row in data.get("periods", {}).items():
        periods[timeframe] = {
            "values": {k: v for k, v in row.get("values", {}).items() if k in fields},
            "closed_bar_confirmed": row.get("closed_bar_confirmed"),
            "unavailable_fields": (
                {k: v for k, v in row["unavailable_fields"].items() if k in fields}
                if isinstance(row.get("unavailable_fields"), dict)
                else [k for k in row.get("unavailable_fields", []) if k in fields]
            ),
        }
    rankings = data.get("market_rankings") or {}
    coin = data.get("coin_context") or {}
    return {
        "source_symbol": data.get("source_provenance", {}).get("verified_source_symbol"),
        "technical_page_unavailable": data.get("source_provenance", {}).get("technical_page_unavailable"),
        "collected_at": data.get("fetched_at"),
        "quote_freshness_verified": data.get("quote_freshness_verified", False),
        "provider_update_mode": data.get("provider_update_mode"),
        "periods": periods,
        "context_metrics": data.get("context_metrics"),
        "market_rankings": {
            "scope_count": rankings.get("scope_count"),
            "metrics": rankings.get("metrics"),
        },
        "community_ideas": [
            {key: idea.get(key) for key in ("title", "excerpt", "card_time", "strategy_label", "same_bybit_perpetual")}
            for idea in data.get("community_ideas", [])[:2]
        ],
        "community_ideas_unavailable": data.get("community_ideas_unavailable"),
        "coin_context": {"scope": coin.get("scope"), "values": coin.get("values")},
        "coin_context_unavailable": data.get("coin_context_unavailable"),
        "news": [
            {key: article.get(key) for key in ("title", "published_at", "provider")}
            for article in data.get("news", [])[:3]
        ],
        "news_unavailable": data.get("news_unavailable"),
        "extra_fields": data.get("extra_fields"),
    }


def compact_bybit(data):
    keep_rows = {"15m": 16, "30m": 12, "1h": 8, "2h": 6, "5m": 12, "1m": 8}
    orderflow = data.get("orderflow")
    if orderflow is not None:
        orderflow = {
            key: value for key, value in orderflow.items()
            if key not in {"book_snapshots", "trade_sample", "sample_volume_at_price"}
        }
    indicators = {}
    for tf in ("15m", "30m", "1h", "2h"):
        row = data.get("indicators", {}).get(tf)
        if row:
            indicators[tf] = {
                "recent_windows": row.get("recent_windows"),
                "closed_samples": row.get("closed_samples"),
                "latest_closed": row.get("recent", [])[-1:],
            }
    price_volume = {}
    for tf, row in data.get("price_volume", {}).items():
        price_volume[tf] = {
            "method": row.get("method"),
            "daily": row.get("daily", [])[-2:],
            "high_volume_candles": row.get("high_volume_candles", [])[:3],
        }
    return {
        "symbol": data["symbol"],
        "observed_at": data["observed_at"],
        "history_hours": data.get("history_hours"),
        "timeframe_roles": data.get("timeframe_roles"),
        "candles": {
            tf: [row for row in rows if row["completed"]][-keep_rows[tf]:]
            for tf, rows in data.get("candles", {}).items() if tf in keep_rows
        },
        "indicators": indicators,
        "price_volume": price_volume,
        "orderflow": orderflow,
    }


class SelectionModels:
    def __init__(self, settings, store, *, tv_runner=None, final_runner=None):
        self.store = store
        self.tv_runner = tv_runner or ModelService(settings, store, TV_SELECTION_PROFILE)
        self.final_runner = final_runner or ModelService(settings, store, FINAL_SELECTION_PROFILE)

    async def choose_tv(self, cid, bundles):
        if not bundles:
            raise ValueError("No valid TradingView discovery evidence")
        symbols = [item["symbol"] for item in bundles]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Duplicate TradingView discovery symbols")
        context = {
            "cycle_id": cid,
            "objective": "未来1—2小时方向，近一周仅背景",
            "candidate_symbols": symbols,
            "bundles": [
                {"symbol": item["symbol"], "discovery": item["discovery"],
                 "tradingview": compact_tradingview(item["tradingview"])}
                for item in bundles
            ],
        }
        self.store.event("TV_SELECTION_EVIDENCE", context)
        schema = TVSelection.model_json_schema()
        strict_schema(schema)
        raw = await self.tv_runner.request(
            "tv_" + identity(cid), context, schema, "tv_selection_v1.md"
        )
        result = TVSelection.model_validate(raw)
        if not {item.symbol for item in result.candidates} <= set(symbols):
            raise ValueError("TradingView model selected a symbol outside the evidence pool")
        self.store.event("TV_SELECTION_RESULT", {"cycle_id": cid, "result": result.model_dump()})
        return sorted(result.candidates, key=lambda item: item.rank)

    async def choose_final(self, cid, bundles):
        if not bundles:
            raise ValueError("No valid Bybit refinement evidence")
        symbols = [item["symbol"] for item in bundles]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Duplicate Bybit refinement symbols")
        context = {
            "cycle_id": cid,
            "objective": "未来1—2小时方向，近一周仅背景",
            "candidate_symbols": symbols,
            "bundles": [
                {"symbol": item["symbol"], "tv_initial": item["tv_initial"],
                 "tradingview": compact_tradingview(item["tradingview"]),
                 "bybit": compact_bybit(item["bybit"])}
                for item in bundles
            ],
        }
        self.store.event("FINAL_SELECTION_EVIDENCE", context)
        schema = FinalSelection.model_json_schema()
        strict_schema(schema)
        raw = await self.final_runner.request(
            "final_" + identity(cid), context, schema, "direction_selection_v1.md"
        )
        result = FinalSelection.model_validate(raw)
        if not {item.symbol for item in result.selected} <= set(symbols):
            raise ValueError("Final model selected a symbol without valid Bybit evidence")
        self.store.event("FINAL_SELECTION_RESULT", {"cycle_id": cid, "result": result.model_dump()})
        return sorted(result.selected, key=lambda item: item.rank)


def final_decision(item: FinalCandidate) -> Decision:
    return Decision(symbol=item.symbol, decision=item.direction, reason=item.reason)
