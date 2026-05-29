"""Worker runtime configuration — pydantic-settings BaseSettings."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Worker runtime settings."""

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
    # Data plane
    # -------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://merlin:merlin@localhost:5432/merlin",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # -------------------------------------------------------------------
    # RQ
    # -------------------------------------------------------------------
    # ``NoDecode`` opts out of pydantic-settings' default JSON pre-decode
    # for complex types, so the env value reaches our ``field_validator``
    # below as the raw string ("default,resolution,reputation") rather
    # than being rejected as malformed JSON. Mirrors the same pattern
    # we use in apps/bot for ``TELEGRAM_ADMIN_CHAT_IDS``.
    rq_queues: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["default", "resolution", "reputation"],
        description=(
            "Queues this worker drains, comma-separated. RQ workers are "
            "queue-scoped. Example: ``default,resolution,reputation``."
        ),
    )
    rq_default_timeout_s: int = Field(default=300, gt=0)

    @field_validator("rq_queues", mode="before")
    @classmethod
    def _split_rq_queues(cls, raw: Any) -> Any:
        """Accept a comma-separated string ("default,resolution") or a list.

        pydantic-settings parses ``list``-typed env vars as JSON by
        default; we override with the friendlier syntax matching the
        ``.env.example`` comment.
        """
        if raw is None or raw == "":
            return ["default", "resolution", "reputation"]
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw

    # -------------------------------------------------------------------
    # External APIs (market data)
    #
    # CoinGecko is the single market-data source per Q-026.
    # The env var is named without
    # the ``_DEMO_`` suffix so the same name carries forward to paid
    # tiers — the base URL discriminates the tier
    # (``api.coingecko.com`` vs. ``pro-api.coingecko.com``).
    # -------------------------------------------------------------------
    coingecko_base_url: str = Field(default="https://api.coingecko.com/api/v3")
    coingecko_api_key: str = Field(
        default="",
        description=(
            "CoinGecko API key. Empty falls back to anonymous public tier "
            "(rate-limited). Same env var works across Demo / Analyst / "
            "Pro tiers — the base URL discriminates."
        ),
    )

    # -------------------------------------------------------------------
    # Telegram (worker sends outcome notifications directly — plan §3)
    # -------------------------------------------------------------------
    telegram_bot_token: str = Field(
        default="",
        description="Bot API token used for outbound resolution notifications.",
    )

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------
    sentry_dsn: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
