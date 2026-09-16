from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEMO_URL = "https://api-demo.bybit.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")
    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    telegram_token: SecretStr = SecretStr("")
    telegram_chat_id: int = 0
    telegram_user_id: int = 0
    runtime_dir: Path = Path("runtime")
    signal_db: Path = Path("/root/single-analysis/runtime/signals.db")
    trading_enabled: bool = False

    def require_credentials(self) -> None:
        if (
            not self.bybit_api_key.get_secret_value()
            or not self.bybit_api_secret.get_secret_value()
        ):
            raise ValueError("Missing BYBIT_API_KEY / BYBIT_API_SECRET in .env")

    def require_telegram(self) -> None:
        if (
            not self.telegram_token.get_secret_value()
            or not self.telegram_chat_id
            or not self.telegram_user_id
        ):
            raise ValueError("Missing TELEGRAM_TOKEN / TELEGRAM_CHAT_ID / TELEGRAM_USER_ID in .env")
