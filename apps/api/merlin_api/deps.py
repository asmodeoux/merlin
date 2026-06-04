"""Shared FastAPI dependencies (cached settings + ports container).

The route handlers depend on a small ``Ports`` container instead of
importing concrete adapters directly. Tests swap the container via
``app.dependency_overrides[get_ports] = lambda: Ports(...)`` to inject
fakes — no monkey-patching, no global state.

The default ``Ports`` factory wires real adapters from ``Settings``:

- ``ClaimDrafterPort`` → ``OpenRouterClaimDrafter`` (or a no-op
  fallback that always raises ``ClaimDrafterError`` when the API key is
  empty — the bot then falls back to the button form).
- ``MarketDataPort`` → ``CoinGeckoAdapter``.
- ``JobQueuePort`` → ``RQQueueAdapter``.
- ``NotifierPort`` → ``TelegramNotifier`` (the API rarely calls this;
  the worker is the main user).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from merlin_api.config import Settings

if TYPE_CHECKING:
    from merlin_core.application.ports import (
        ClaimDrafterPort,
        JobQueuePort,
        MarketDataPort,
        NotifierPort,
    )
    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide singleton Settings.

    `BaseSettings` reads from the env on first call. Tests override this
    via `app.dependency_overrides[get_settings] = lambda: Settings(...)`.
    """
    return Settings()


@dataclass
class Ports:
    """Container for every port the API route handlers may need.

    Keeping this as a single injectable simplifies the test override
    surface — one ``app.dependency_overrides[get_ports]`` swap and every
    route sees fakes.

    ``redis`` is an *optional* async client used by the response-cache
    helpers in ``merlin_api.cache``. Routes that don't need caching
    leave it untouched; routes that do need caching pass it to
    ``cached_or_compute`` which fail-opens to the upstream when the
    client is ``None``. Tests can override the whole ``Ports`` instance
    with ``redis=None`` (cache disabled) or with a ``fakeredis`` client
    (cache exercised).
    """

    drafter: ClaimDrafterPort
    market_data: MarketDataPort
    queue: JobQueuePort
    notifier: NotifierPort
    redis: AsyncRedis | None = None


@lru_cache(maxsize=1)
def get_ports() -> Ports:
    """Build the default ``Ports`` from ``Settings``.

    Imports are deferred so unit tests of unrelated modules don't pull
    in httpx + redis on collection. The cache means we only construct
    one set of adapters per process.

    NOTE: ``Settings`` is fetched via ``get_settings()`` directly rather
    than as a ``Depends`` parameter. Combining ``@lru_cache`` with a
    ``Depends(...)`` arg crashes FastAPI's dependency solver at request
    time with ``TypeError: unhashable type: 'Settings'`` (Pydantic v2
    models are mutable, hence unhashable, and ``lru_cache`` tries to
    hash all arguments for memoization). ``get_settings()`` is itself
    ``@lru_cache``'d so calling it here still yields the same singleton
    across every request. Tests override via
    ``app.dependency_overrides[get_ports] = lambda: Ports(...)`` and so
    bypass this construction path entirely — which is why this only
    surfaced under live traffic.
    """
    import httpx
    from merlin_infrastructure.llm.openrouter import OpenRouterClaimDrafter
    from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.queue.names import RESOLUTION_QUEUE_NAME
    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedisClient

    settings = get_settings()

    return Ports(
        drafter=OpenRouterClaimDrafter(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            primary_model=settings.llm_primary_model,
            fallback_model=settings.llm_fallback_model,
            http_client=httpx.AsyncClient(timeout=15.0),
        ),
        market_data=CoinGeckoAdapter(
            api_key=settings.coingecko_api_key or None,
            base_url=settings.coingecko_base_url,
            http_client=httpx.AsyncClient(timeout=15.0),
        ),
        queue=RQQueueAdapter(
            redis=Redis.from_url(settings.redis_url),
            # ``POST /claims`` schedules ``resolve_outcome`` jobs on
            # the dedicated ``resolution`` queue (one of the names the
            # worker drains by default). Passing the constant — rather
            # than letting the adapter silently default — closes the
            # Sprint-1 mismatch regression where the API talked to
            # ``"merlin"`` while the worker listened on ``resolution``.
            queue_name=RESOLUTION_QUEUE_NAME,
        ),
        notifier=TelegramNotifier(
            bot_token=settings.telegram_bot_token or "noop",
            http_client=httpx.AsyncClient(timeout=10.0),
        ),
        # Async Redis client for the response cache (see merlin_api.cache).
        # Lazy connection — from_url doesn't touch the network until the
        # first command runs, so unit tests that don't hit the cache path
        # never need a live Redis.
        redis=AsyncRedisClient.from_url(settings.redis_url),
    )


# ---------------------------------------------------------------------------
# DB session dependency — one ``AsyncSession`` per request.
# ---------------------------------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` for the request.

    The route handler holds the session for the lifetime of the
    request; repositories layer their flushes on top, and the handler
    COMMITs (or the dependency rolls back on exception). See
    ``merlin_infrastructure.db.session.yield_session`` for the
    contract — kept thin here so tests can override with a single
    ``app.dependency_overrides`` swap.
    """
    from merlin_infrastructure.db.session import (
        get_session_factory,
        yield_session,
    )

    settings = get_settings()
    factory = get_session_factory(settings.database_url)
    async for session in yield_session(factory):
        yield session
