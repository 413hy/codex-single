from datetime import datetime

import pytest


def test_liquidation_event_cannot_advance_coverage_and_unordered_expiry_is_pruned():
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from analysis_core.vendor.signal.providers.public_streams import (
        PublicLiquidation,
        PublicStreamCache,
    )

    now = datetime.now(UTC)
    cache = PublicStreamCache(retention_seconds=600)
    cache.mark_subscribed("TESTUSDT", at=now - timedelta(seconds=900))

    def event(at):
        return PublicLiquidation(
            symbol="TESTUSDT",
            timestamp=at,
            liquidated_position="LONG",
            price=Decimal(1),
            size=Decimal(1),
        )

    cache.record_liquidation(event(now))
    cache.record_liquidation(event(now - timedelta(seconds=700)))
    window = cache.liquidation_window("TESTUSDT", now=now)
    assert not window.coverage_complete
    assert len(cache._events["TESTUSDT"]) == 1
    with pytest.raises(ValueError, match="Future"):
        cache.record_liquidation(event(now + timedelta(days=1)))
    cache.heartbeat("TESTUSDT", at=now)
    assert cache.liquidation_window("TESTUSDT", now=now).coverage_complete


async def test_benchmark_returns_exclude_candles_after_frozen_cutoff():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from analysis_core.vendor.signal.providers.market_context import MarketContextCollector

    cutoff = datetime(2026, 9, 11, 8, tzinfo=UTC)

    class Client:
        async def completed_candles(self, symbol, timeframe, limit):
            duration = timedelta(minutes={"3m": 3, "5m": 5, "15m": 15}[timeframe])
            return [
                SimpleNamespace(
                    symbol=symbol,
                    timeframe=timeframe,
                    completed=True,
                    open_time=cutoff + duration * (i - 2),
                    close_time=cutoff + duration * (i - 1),
                    close=100 * (i + 1),
                )
                for i in range(3)
            ]

    values = await MarketContextCollector(Client())._benchmark_returns("BTCUSDT", cutoff)
    assert values == {"3m": 100.0, "5m": 100.0, "15m": 100.0}


@pytest.mark.parametrize("minutes", [0, -5, 3, 7])
def test_scanner_rejects_duplicate_reversed_or_fractional_intervals(minutes):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from analysis_core.vendor.scanner import _missing_intervals

    at = datetime(2026, 9, 11, tzinfo=UTC)
    with pytest.raises(ValueError, match="Invalid scanner"):
        _missing_intervals(
            (
                SimpleNamespace(open_time=at),
                SimpleNamespace(open_time=at + timedelta(minutes=minutes)),
            )
        )
    assert (
        _missing_intervals(
            (SimpleNamespace(open_time=at), SimpleNamespace(open_time=at + timedelta(minutes=15)))
        )
        == 2
    )


def test_unused_stream_component_rejects_naive_time_at_its_boundary():
    from analysis_core.vendor.signal.providers.public_streams import (
        PublicLiquidation,
        PublicStreamCache,
    )

    naive = datetime(2026, 9, 9)
    with pytest.raises(ValueError, match="timezone-aware"):
        PublicLiquidation(
            symbol="TESTUSDT", timestamp=naive, liquidated_position="LONG", price=1, size=1
        )
    cache = PublicStreamCache()
    for method in (cache.mark_subscribed, cache.heartbeat):
        with pytest.raises(ValueError, match="timezone-aware"):
            method("TESTUSDT", at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.liquidation_window("TESTUSDT", now=naive)
