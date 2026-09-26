from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SCREENING_MODEL = "gpt-5.6-terra"
SCREENING_REASONING = "medium"
DIRECTION_MODEL = "gpt-6-sol"
DIRECTION_REASONING = "medium"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")
    telegram_token: SecretStr = SecretStr("")
    telegram_chat_id: int = 0
    telegram_user_id: int = 0
    tradingview_enabled: bool = False
    tradingview_extra_fields: str = ""
    codex_bin: str = "/root/.local/bin/codex"
    runtime_dir: Path = Path("runtime")
    hedge_db: Path = Path("/root/auto-trader-longtime-02/runtime/trader.db")
    model_timeout: int = Field(default=300, ge=30, le=600)
