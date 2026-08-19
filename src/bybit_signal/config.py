from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(StrictModel):
    mode: Literal["production", "shadow", "no_notify"] = "production"
    data_root: Path = Path("runtime/data")
    log_root: Path = Path("runtime/logs")
    database_path: Path = Path("runtime/state/signal.db")
    market_request_concurrency: int = Field(default=8, ge=1, le=20)


class AnalysisConfig(StrictModel):
    interval_minutes: Literal[30] = 30
    top_candidates: Literal[5] = 5
    max_strong_signals: int = Field(default=2, ge=1, le=2)
    primary_signal_count: Literal[2] = 2
    model: str = "gpt-5.6-terra"
    reasoning_effort: Literal["medium", "high"] = "medium"
    timeout_seconds: int = Field(default=300, ge=60, le=900)
    max_attempts: int = Field(default=2, ge=1, le=3)
    monitoring_review_repair_attempts: int = Field(default=3, ge=0, le=3)
    tool_protocol_enabled: bool = True
    max_tool_rounds: int = Field(default=2, ge=0, le=3)
    max_tool_calls: int = Field(default=8, ge=0, le=24)
    tool_timeout_seconds: int = Field(default=12, ge=2, le=60)
    strategy_version: str = Field(default="ultrashort-v5", min_length=3, max_length=80)
    prompt_version: str = Field(default="signal-analysis-v9", min_length=3, max_length=80)


class BybitConfig(StrictModel):
    rest_base_url: str = "https://api.bybit.com"
    public_linear_ws_url: str = "wss://stream.bybit.com/v5/public/linear"

    @model_validator(mode="after")
    def public_only(self) -> BybitConfig:
        if "/private" in self.public_linear_ws_url or "/trade" in self.public_linear_ws_url:
            raise ValueError("only Bybit public market WebSocket is allowed")
        return self


class PublicSourcesConfig(StrictModel):
    cross_exchange_enabled: bool = True
    binance_futures_base_url: str = "https://fapi.binance.com"
    okx_base_url: str = "https://www.okx.com"


class ScannerConfig(StrictModel):
    preselect_limit: int = Field(default=60, ge=5, le=200)
    candle_limit: int = Field(default=72, ge=49, le=300)
    request_concurrency: int = Field(default=8, ge=1, le=20)
    minimum_24h_turnover_usdt: float = Field(default=1_500_000, ge=0)
    minimum_recent_30m_turnover_usdt: float = Field(default=50_000, ge=0)
    maximum_spread_bps: float = Field(default=25, gt=0, le=500)
    minimum_completed_candles: int = Field(default=48, ge=24, le=200)
    maximum_missing_intervals: int = Field(default=2, ge=0, le=12)
    minimum_median_range_percent: float = Field(default=0.15, ge=0, le=100)
    minimum_max_return_percent: float = Field(default=0.35, ge=0, le=100)
    depth_check_limit: int = Field(default=20, ge=5, le=60)
    thin_depth_notional_usdt: float = Field(default=5_000, ge=0)
    choppy_overlap_ratio: float = Field(default=0.72, ge=0, le=1)
    impact_extension_atr: float = Field(default=2.5, gt=0, le=20)
    minimum_tradability_score: float = Field(default=18, ge=0, le=100)


class MonitoringConfig(StrictModel):
    enabled: bool = True
    soft_model_wakes_per_hour: int = Field(default=10, ge=1, le=60)
    directive_refresh_seconds: int = Field(default=30, ge=10, le=300)
    reconnect_delay_seconds: int = Field(default=5, ge=1, le=60)
    maximum_directives_per_symbol: int = Field(default=3, ge=1, le=3)
    microstructure_confirmation_seconds: int = Field(default=3, ge=1, le=30)


class FreshnessConfig(StrictModel):
    enabled: bool = True


class TelegramConfig(StrictModel):
    enabled: bool = False
    token: str = Field(default="", repr=False)
    allowed_chat_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    api_base_url: str = "https://api.telegram.org"
    polling_timeout_seconds: int = Field(default=25, ge=1, le=50)
    delivery_enabled: bool = True

    @model_validator(mode="after")
    def validate_enabled(self) -> TelegramConfig:
        if self.enabled and not self.token:
            raise ValueError("enabled Telegram notifications require an environment token")
        if self.enabled and (not self.allowed_chat_ids or not self.allowed_user_ids):
            raise ValueError("enabled Telegram notifications require chat and user allowlists")
        return self


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BYBIT_SIGNAL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    schema_version: Literal[1] = 1
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    runtime: RuntimeConfig = RuntimeConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    bybit: BybitConfig = BybitConfig()
    public_sources: PublicSourcesConfig = PublicSourcesConfig()
    scanner: ScannerConfig = ScannerConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    freshness: FreshnessConfig = FreshnessConfig()
    telegram: TelegramConfig = TelegramConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> AppSettings:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("configuration root must be an object")
        return cls(**document)
