from pathlib import Path

import pytest
from pydantic import ValidationError

from bybit_signal.config import AppSettings, BybitConfig, TelegramConfig


def test_example_configuration_loads() -> None:
    settings = AppSettings.from_yaml(Path("config/system.example.yaml"))
    assert settings.analysis.interval_minutes == 30
    assert settings.analysis.model == "gpt-5.6-terra"
    assert settings.analysis.reasoning_effort == "medium"
    assert settings.analysis.timeout_seconds == 300
    assert settings.analysis.monitoring_review_repair_attempts == 3
    assert settings.analysis.primary_signal_count == 2
    assert settings.scanner.minimum_24h_turnover_usdt == 1_500_000
    assert settings.scanner.minimum_recent_30m_turnover_usdt == 50_000
    assert settings.scanner.maximum_spread_bps == 25
    assert settings.telegram.enabled is False


def test_private_bybit_websocket_is_rejected() -> None:
    with pytest.raises(ValidationError, match="public market"):
        BybitConfig(public_linear_ws_url="wss://stream.bybit.com/v5/private")


def test_enabled_telegram_requires_token_and_allowlists() -> None:
    with pytest.raises(ValidationError, match="environment token"):
        TelegramConfig(enabled=True)
