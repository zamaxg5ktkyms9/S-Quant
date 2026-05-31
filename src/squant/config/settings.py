from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # populate_by_name=True so tests can initialise via Python field names
    # (e.g. Settings(bypass_execution_time_guard=True)) in addition to env aliases.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Google Sheets — service account JSON as a single env var (GitHub Secrets friendly)
    gcp_sa_key_json: str = Field(default="", alias="GCP_SA_KEY_JSON")
    spreadsheet_id: str = Field(default="", alias="SPREADSHEET_ID")

    # J-Quants v2 (JPX official market data — API key from dashboard)
    jquants_api_key: str = Field(default="", alias="JQUANTS_API_KEY")
    jquants_rpm: int = Field(default=50, alias="JQUANTS_RPM")

    # Slack
    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")

    # Trading overrides
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    # Test-only guard bypasses: workflow_dispatch can flip these to true to
    # exercise the full pipeline outside trading hours / on weekends. cron
    # never sets them, so the scheduled production run is unaffected.
    bypass_execution_time_guard: bool = Field(
        default=False, alias="BYPASS_EXECUTION_TIME_GUARD"
    )
    bypass_trading_day_check: bool = Field(
        default=False, alias="BYPASS_TRADING_DAY_CHECK"
    )
    budget_jpy: Decimal = Field(default=Decimal("200000"), alias="BUDGET_JPY")  # Phase 1 = ¥200k (C strategy)
    max_positions: int = Field(default=2, alias="MAX_POSITIONS")  # Phase 1 = 2 banks
    signal_strategy: str = Field(default="ma_cross", alias="SIGNAL_STRATEGY")  # "pullback" | "ma_cross"
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
