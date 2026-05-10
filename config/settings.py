"""Project-wide settings loaded from .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API keys
    fugle_api_key: str = Field(default="", alias="FUGLE_API_KEY")
    finmind_token: str = Field(default="", alias="FINMIND_TOKEN")

    # Discord
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")

    # Runtime
    tz: str = Field(default="Asia/Taipei", alias="TZ")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Screener
    screener_lookback_days: int = Field(default=20, alias="SCREENER_LOOKBACK_DAYS")
    screener_min_avg_volume_lots: int = Field(
        default=10_000, alias="SCREENER_MIN_AVG_VOLUME_LOTS"
    )
    screener_min_turnover_rate: float = Field(
        default=0.02, alias="SCREENER_MIN_TURNOVER_RATE"
    )
    screener_min_atr_pct: float = Field(default=0.025, alias="SCREENER_MIN_ATR_PCT")
    screener_price_min: float = Field(default=10.0, alias="SCREENER_PRICE_MIN")
    screener_price_max: float = Field(default=500.0, alias="SCREENER_PRICE_MAX")
    screener_top_n: int = Field(default=30, alias="SCREENER_TOP_N")


settings = Settings()
