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

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from fastapi import Depends

from merlin_api.config import Settings

if TYPE_CHECKING:
    from merlin_core.application.ports import (
        ClaimDrafterPort,
        JobQueuePort,
        MarketDataPort,
        NotifierPort,
    )


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
    """

    drafter: ClaimDrafterPort
    market_data: MarketDataPort
    queue: JobQueuePort
    notifier: NotifierPort


@lru_cache(maxsize=1)
def get_ports(settings: Settings = Depends(get_settings)) -> Ports:
    """Build the default ``Ports`` from ``Settings``.

    Imports are deferred so unit tests of unrelated modules don't pull
    in httpx + redis on collection. The cache means we only construct
    one set of adapters per process.
    """
    import httpx
    from merlin_infrastructure.llm.openrouter import OpenRouterClaimDrafter
    from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis

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
        ),
        notifier=TelegramNotifier(
            bot_token=settings.telegram_bot_token or "noop",
            http_client=httpx.AsyncClient(timeout=10.0),
        ),
    )
