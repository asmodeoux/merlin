"""Runtime configuration — pydantic-settings BaseSettings.

Every env var the API reads is declared here. `scripts/env_check.py`
(and `make env-check`, and a CI step) compares this Settings class to
`apps/api/.env.example` and fails the build on mismatch — adding /
renaming an env var requires updating both in the same commit.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """API runtime settings."""

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
    log_level: str = Field(default="INFO", description="INFO / DEBUG / WARNING / ERROR")

    # -------------------------------------------------------------------
    # Data plane
    # -------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://merlin:merlin@localhost:5432/merlin",
        description="Async Postgres DSN (asyncpg driver).",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis DSN — RQ queues + rate limiter.",
    )

    # -------------------------------------------------------------------
    # Auth (v0 — HMAC bearer for service-to-service)
    # -------------------------------------------------------------------
    merlin_api_internal_token: str = Field(
        default="dev-internal-token-do-not-use-in-prod",
        description=(
            "HMAC bearer token. Bot + worker read the same value from "
            "their own .env and send it as `Authorization: Bearer <token>`."
        ),
    )

    # -------------------------------------------------------------------
    # Observability
    # -------------------------------------------------------------------
    sentry_dsn: str = Field(default="", description="Sentry DSN; empty disables.")
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # -------------------------------------------------------------------
    # API metadata (surfaced in OpenAPI)
    # -------------------------------------------------------------------
    api_title: str = Field(default="Merlin API")
    api_version: str = Field(default="0.1.0")
