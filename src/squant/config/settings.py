from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Google Sheets — service account JSON as a single env var (GitHub Secrets friendly)
    gcp_sa_key_json: str = Field(default="", alias="GCP_SA_KEY_JSON")
    spreadsheet_id: str = Field(default="", alias="SPREADSHEET_ID")

    # Slack
    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")

    # Trading overrides
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    budget_jpy: Decimal = Field(default=Decimal("100000"), alias="BUDGET_JPY")
    gap_up_threshold: Decimal = Field(default=Decimal("0.02"), alias="GAP_UP_THRESHOLD")
    stop_loss_rate: Decimal = Field(default=Decimal("0.025"), alias="STOP_LOSS_RATE")
    time_stop_days: int = Field(default=5, alias="TIME_STOP_DAYS")
    circuit_breaker_loss_jpy: Decimal = Field(
        default=Decimal("30000"), alias="CIRCUIT_BREAKER_LOSS_JPY"
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
