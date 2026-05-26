"""Bot runtime configuration — pydantic-settings BaseSettings.

Every env var the bot reads is declared here. Adding / renaming an env
var requires updating both this class and `apps/bot/.env.example` in the
same commit (`make env-check` enforces).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bot runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # -------------------------------------------------------------------
    # Environment
    # -------------------------------------------------------------------
    environment: str = Field(default="dev", description="dev / staging / prod")
    log_level: str = Field(default="INFO")

    # -------------------------------------------------------------------
    # Telegram
    # -------------------------------------------------------------------
    telegram_bot_token: str = Field(
        default="",
        description="BotFather token. Empty disables the bot (logs a warning).",
    )
    telegram_admin_chat_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Comma-separated admin chat IDs. Used for admin-scoped commands "
            "via BotCommandScopeChat."
        ),
    )
    telegram_mode: Literal["polling", "webhook"] = Field(default="polling")
    telegram_webhook_url: str = Field(default="", description="Required when mode=webhook.")
    telegram_webhook_secret: str = Field(
        default="",
        description="TG webhook secret_token (Application.run_webhook(secret_token=...)).",
    )

    # -------------------------------------------------------------------
    # API
    # -------------------------------------------------------------------
    merlin_api_base_url: str = Field(default="http://merlin-api:8000")
    merlin_api_internal_token: str = Field(
        default="dev-internal-token-do-not-use-in-prod",
        description="HMAC bearer for service-to-service calls to merlin-api.",
    )
    merlin_api_timeout_s: float = Field(default=10.0, gt=0)

    # -------------------------------------------------------------------
    # Persistence (Redis-backed BasePersistence)
    # -------------------------------------------------------------------
    redis_url: str = Field(default="redis://localhost:6379/1")

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------
    sentry_dsn: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
