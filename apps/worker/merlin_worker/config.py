"""Worker runtime configuration — pydantic-settings BaseSettings."""

from __future__ import annotations

from typing import Annotated, Any

from merlin_infrastructure.queue.names import ALL_QUEUE_NAMES
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
        default_factory=lambda: list(ALL_QUEUE_NAMES),
        description=(
            "Queues this worker drains, comma-separated. RQ workers are "
            "queue-scoped. Default mirrors "
            "``merlin_infrastructure.queue.names.ALL_QUEUE_NAMES`` "
            "(``default,resolution,reputation``) so the worker always "
            "drains every queue the API + resolver enqueue on. "
            "Preflight tests cross-check this list against the "
            "queue names the producers pass to ``RQQueueAdapter``."
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
            return list(ALL_QUEUE_NAMES)
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw

    # -------------------------------------------------------------------
    # Resolution-sweep cron (Q-030) — durable backstop for lost RQ jobs.
    #
    # The ``POST /claims`` path schedules resolution as a single RQ
    # ``enqueue_at`` job parked in Redis for the whole horizon. Prod
    # Redis (Upstash) is eviction-enabled, so that key can be dropped
    # under memory pressure (and an OOM-SIGKILL / Redis restart can lose
    # it too). The durable ``claims`` row is the only surviving record;
    # this cron periodically reconciles Postgres against the queue and
    # re-fires anything stranded. See ``schedules/cron.py``.
    # -------------------------------------------------------------------
    resolution_sweep_enabled: bool = Field(
        default=True,
        description=(
            "Whether the worker starts the resolution-reconciliation "
            "cron thread. Default on — the durable backstop (Q-030). "
            "Set false only to silence the sweep on a dev box."
        ),
    )
    resolution_sweep_interval_s: int = Field(
        default=900,
        gt=0,
        description="Seconds between resolution sweeps (default 15 min).",
    )
    resolution_sweep_grace_s: int = Field(
        default=21_600,
        ge=0,
        description=(
            "How far past ``resolves_at`` a claim must be before the "
            "sweep re-enqueues it (default 6h — MVP §12.1). Keeps the "
            "sweep off bets RQ's own enqueue_at + Retry window still owns."
        ),
    )
    resolution_sweep_max_batch: int = Field(
        default=500,
        gt=0,
        description="Max claims re-enqueued per single sweep tick.",
    )

    # -------------------------------------------------------------------
    # First-call engagement nudge cron (Q-031).
    #
    # DMs onboarded testers who have a name but zero calls, prompting
    # their first bet. Per-user limits live in Postgres
    # (``engagement_nudges``), not Redis. See
    # ``jobs/first_call_nudge.py`` + ``schedules/cron.py``.
    # -------------------------------------------------------------------
    first_call_nudge_enabled: bool = Field(
        default=True,
        description=(
            "Whether the worker runs the first-call nudge cron. Set "
            "false to silence activation DMs (e.g. on a dev box)."
        ),
    )
    first_call_nudge_interval_s: int = Field(
        default=86_400,
        gt=0,
        description="Seconds between nudge sweeps (default daily).",
    )
    first_call_nudge_cooldown_s: int = Field(
        default=604_800,
        gt=0,
        description=(
            "Minimum seconds between nudges to the same user (default "
            "7 days) — the 'no more than once a week' rule."
        ),
    )
    first_call_nudge_max_sends: int = Field(
        default=2,
        gt=0,
        description="Lifetime cap on first-call nudges per user.",
    )
    first_call_nudge_min_account_age_s: int = Field(
        default=86_400,
        ge=0,
        description=(
            "Don't nudge an account younger than this (default 1 day) "
            "— a settle-in guard so we never ping someone mid-first-"
            "call seconds after they set their name. Set 0 to disable."
        ),
    )

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
