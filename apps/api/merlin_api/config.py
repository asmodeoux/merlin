"""Runtime configuration — pydantic-settings BaseSettings.

Every env var the API reads is declared here. `scripts/env_check.py`
(and `make env-check`, and a CI step) compares this Settings class to
`apps/api/.env.example` and fails the build on mismatch — adding /
renaming an env var requires updating both in the same commit.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # LLM (OpenRouter — single freeform-LLM ingest path, Q-028 proposal)
    # -------------------------------------------------------------------
    openrouter_api_key: str = Field(
        default="",
        description="OpenRouter bearer token; empty disables the freeform path.",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter API base URL.",
    )
    llm_primary_model: str = Field(
        default="openai/gpt-5-mini",
        description="First-choice OpenRouter model slug.",
    )
    llm_fallback_model: str = Field(
        default="google/gemini-3.5-flash",
        description="Fallback model when the primary returns 5xx.",
    )
    llm_monthly_usd_budget: float = Field(
        default=30.0,
        ge=0.0,
        description=(
            "Soft monthly USD cap; when exceeded the API short-circuits "
            "freeform drafts and the bot falls back to the button form."
        ),
    )

    # -------------------------------------------------------------------
    # Market data (CoinGecko — single source per Q-026)
    # -------------------------------------------------------------------
    coingecko_api_key: str = Field(
        default="",
        description=(
            "CoinGecko API key. Empty falls back to anonymous public tier "
            "(rate-limited). Same env var carries forward to paid tiers — "
            "the base URL discriminates the tier."
        ),
    )
    coingecko_base_url: str = Field(
        default="https://api.coingecko.com/api/v3",
        description="CoinGecko REST base URL.",
    )

    # -------------------------------------------------------------------
    # Telegram (notifier in the worker; this default is here so the API
    # can ship the same Settings shape to `make env-check`).
    # -------------------------------------------------------------------
    telegram_bot_token: str = Field(
        default="",
        description="Bot API token. Worker uses this for outbound notifications.",
    )

    # -------------------------------------------------------------------
    # API metadata (surfaced in OpenAPI)
    # -------------------------------------------------------------------
    api_title: str = Field(default="Merlin API")
    api_version: str = Field(default="0.2.0")

    # -------------------------------------------------------------------
    # Admin allowlist (parallels merlin_bot — same env var on both sides
    # so the admin's Telegram ID is recognised by every service).
    #
    # Used by ``POST /sources/admin_bootstrap`` and ``POST /invites`` to
    # gate admin-only endpoints. The bot also reads this list so its
    # ``/start`` and ``/invite`` handlers know which user qualifies.
    #
    # ``NoDecode`` + the validator below let the env var stay a
    # comma-separated string (``123,456``) — the same trap that ate the
    # worker at boot (preflight test in ``tests/preflight``).
    # -------------------------------------------------------------------
    telegram_admin_chat_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description=(
            "Comma-separated Telegram chat IDs that bypass invite redemption "
            "and may issue new invite codes. Same env shape as merlin_bot."
        ),
    )

    @field_validator("telegram_admin_chat_ids", mode="before")
    @classmethod
    def _parse_admin_chat_ids(cls, raw: object) -> list[int]:
        if raw is None or raw == "":
            return []
        if isinstance(raw, list):
            return [int(x) for x in raw]
        if isinstance(raw, str):
            return [int(part) for part in (p.strip() for p in raw.split(",")) if part]
        msg = f"Cannot parse telegram_admin_chat_ids from {raw!r}"
        raise TypeError(msg)
