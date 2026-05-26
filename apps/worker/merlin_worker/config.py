"""Worker runtime configuration — pydantic-settings BaseSettings."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    rq_queues: list[str] = Field(
        default_factory=lambda: ["default", "resolution", "reputation"],
        description="Queues this worker drains. RQ workers are queue-scoped.",
    )
    rq_default_timeout_s: int = Field(default=300, gt=0)

    # -------------------------------------------------------------------
    # External APIs (market data)
    #
    # CoinGecko Demo is the single market-data source per Q-026.
    # The free tier doesn't require
    # an API key; if we later upgrade to Analyst ($129/mo) we'll add
    # `coingecko_api_key` here and switch to `https://pro-api.coingecko.com`.
    # -------------------------------------------------------------------
    coingecko_base_url: str = Field(default="https://api.coingecko.com")
    coingecko_demo_api_key: str = Field(
        default="",
        description="Optional Demo-tier key. Empty = anonymous (lower rate limit).",
    )

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------
    sentry_dsn: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
