from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bybit_signal.cmi.models import CmiSnapshot
from bybit_signal.domain.enums import PriceType, ToolStatus
from bybit_signal.evidence.builder import EvidenceBuilder


def _candles(symbol: str, timeframe: str, interval_ms: int) -> dict[str, Any]:
    base_time = 1_786_970_000_000
    completed = []
    for index in range(60):
        center = 100 + index * 0.2 + (index % 5 - 2) * 0.1
        completed.append(
            {
                "normalized_symbol": symbol,
                "interval": timeframe,
                "complete": True,
                "gap_detected": False,
                "is_stale": False,
                "open_time_ms": base_time + index * interval_ms,
                "open": center - 0.1,
                "high": center + 0.4,
                "low": center - 0.5,
                "close": center + 0.1,
            }
        )
    return {
        "available": True,
        "quality_status": "QUALIFIED",
        "completed": completed,
        "forming": [
            {
                "normalized_symbol": symbol,
                "interval": timeframe,
                "complete": False,
                "open_time_ms": base_time + 60 * interval_ms,
                "open": 100,
                "high": 999_999,
                "low": 0.0001,
                "close": 999_999,
            }
        ],
    }


def _snapshot(tmp_path: Path) -> CmiSnapshot:
    symbol = "CYSUSDT"
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    payload: dict[str, Any] = {
        "perpetual_tickers": {
            "bybit": {
                "normalized_symbol": symbol,
                "last_price": 111.9,
                "mark_price": 112.1,
                "index_price": 112.0,
                "last_price_time_ms": now_ms,
                "mark_price_time_ms": now_ms - 100,
                "bid_price": 111.8,
                "ask_price": 112.0,
                "funding_rate": 0.0001,
                "turnover_24h": 1_000_000,
                "open_interest_value": 2_000_000,
            }
        },
        "candles": {
            "bybit": {
                "5m": _candles(symbol, "5m", 300_000),
                "15m": _candles(symbol, "15m", 900_000),
                "1h": _candles(symbol, "1h", 3_600_000),
                "4h": _candles(symbol, "4h", 14_400_000),
            }
        },
        "technical_indicators": {"bybit": {"perp": {}}},
        "order_flow": {
            "bybit": {
                "perp": {
                    "1m": {
                        "available": True,
                        "coverage_complete": True,
                        "partial": False,
                        "quality_status": "QUALIFIED",
                        "status": "COMPLETE",
                        "stream_gap_detected": False,
                        "truncated_by_api_limit": False,
                        "coverage_ratio": 1.0,
                        "sample_count": 20,
                        "window_buy_notional": 700,
                        "window_sell_notional": 300,
                        "window_delta": 400,
                        "buy_sell_ratio": 2.333,
                        "large_trade_count": 1,
                    },
                    "5m": {
                        "available": True,
                        "coverage_complete": False,
                        "partial": True,
                        "quality_status": "PARTIAL",
                        "status": "WARMING_UP",
                        "stream_gap_detected": False,
                        "truncated_by_api_limit": True,
                        "coverage_ratio": 0.4,
                        "sample_count": 30,
                        "window_delta": 9_999_999,
                    },
                }
            }
        },
        "orderbook": {
            "bybit:perp": {
                "available": True,
                "partial": False,
                "quality_status": "QUALIFIED",
                "sequence_healthy": True,
                "snapshot_healthy": True,
                "stream_healthy": True,
                "spread_bps": 2.5,
                "resync_count": 0,
                "sequence_gap_count": 0,
                "level_imbalance": {"top5": 0.2, "top20": 0.1},
                "depth": {
                    "10bps": {"bid_notional": 5_000, "ask_notional": 4_000}
                },
            }
        },
        "open_interest": {
            "bybit": {
                "15m": {
                    "available": True,
                    "coverage_complete": True,
                    "quality_status": "QUALIFIED",
                    "percent_change": 0.02,
                }
            }
        },
        "liquidations": {
            "5m": {
                "available": True,
                "coverage_complete": False,
                "partial": True,
                "long_liquidation_notional": 9_999_999,
            }
        },
    }
    return CmiSnapshot(
        symbol=symbol,
        schema_version="2.0",
        generated_at=now,
        captured_at=now,
        json_path=tmp_path / "snapshot.json",
        text_path=tmp_path / "snapshot.txt",
        health_path=tmp_path / "health.json",
        sha256="a" * 64,
        status=ToolStatus.PARTIAL,
        snapshot_status="USABLE_WITH_PARTIAL_WINDOWS",
        completeness_status="INCOMPLETE",
        health_status="DEGRADED",
        application_version="2.2.1",
        available_perpetual_exchanges=("bybit",),
        limitation_count=1,
        limitation_summaries=("test limitation",),
        payload=payload,
    )


def test_builder_preserves_canonical_prices_and_excludes_forming_candles(
    tmp_path: Path,
) -> None:
    bundle = EvidenceBuilder().build(_snapshot(tmp_path))

    assert bundle.canonical_last.price_type is PriceType.LAST
    assert bundle.canonical_last.value != bundle.canonical_mark.value
    price_action = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.PA.5M"
    )
    assert price_action.values["completed_candles"] == 60
    assert float(price_action.values["rolling_high_20"]) < 1_000
    assert float(price_action.values["rolling_low_20"]) > 1


def test_builder_fail_closes_partial_order_flow_and_liquidations(tmp_path: Path) -> None:
    bundle = EvidenceBuilder().build(_snapshot(tmp_path))

    qualified = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.OF.1M"
    )
    partial = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.OF.5M"
    )
    derivatives = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.DERIVATIVES"
    )
    assert qualified.values["normalized_delta"] == 0.4
    assert partial.values["qualified"] is False
    assert "window_delta" not in partial.values
    assert derivatives.values["liquidations_5m_qualified"] is False
    assert "long_liquidation_notional_5m" not in derivatives.values


def test_confirmed_pivots_never_use_a_future_or_forming_bar(tmp_path: Path) -> None:
    bundle = EvidenceBuilder().build(_snapshot(tmp_path))
    price_action = next(
        item for item in bundle.evidence_items if item.evidence_id == "CYSUSDT.PA.5M"
    )

    latest_time = int(price_action.values["latest_completed_open_time_ms"])
    for key in ("swing_high_confirmed_at_ms", "swing_low_confirmed_at_ms"):
        confirmed_at = price_action.values[key]
        assert confirmed_at is None or int(confirmed_at) <= latest_time
