"""Bot runtime configuration — pydantic-settings BaseSettings.

Every env var the bot reads is declared here. Adding / renaming an env
var requires updating both this class and `apps/bot/.env.example` in the
same commit (`make env-check` enforces).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # ``NoDecode`` opts out of pydantic-settings' default JSON pre-decode
    # for complex types, so the env value reaches our ``field_validator``
    # below as the raw string ("123,456" or just "123").
    telegram_admin_chat_ids: Annotated[list[int], NoDecode] = Field(
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

    @field_validator("telegram_admin_chat_ids", mode="before")
    @classmethod
    def _split_admin_chat_ids(cls, raw: Any) -> Any:
        """Accept a comma-separated string ("123,456") or a single int.

        pydantic-settings parses list-typed env vars as JSON by default;
        we override with a friendlier syntax matching the .env.example
        documentation ("comma-separated chat IDs").
        """
        if raw is None or raw == "":
            return []
        if isinstance(raw, str):
            return [int(part) for part in raw.split(",") if part.strip()]
        return raw
