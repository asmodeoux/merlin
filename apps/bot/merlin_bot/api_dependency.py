"""Inject runtime singletons into ``application.bot_data`` at startup.

MerlinClient + admin allowlist + Redis. Handlers retrieve them via
``context.bot_data["..."]``. No globals. The same httpx connection
pool serves the whole process; the same Redis client (async) backs
the rate-limit middleware buckets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from merlin_client import MerlinClient
from redis.asyncio import Redis

if TYPE_CHECKING:
    from telegram.ext import Application

    from merlin_bot.config import Settings


BOT_DATA_KEY = "merlin_client"
BOT_DATA_ADMIN_KEY = "admin_chat_ids"
BOT_DATA_REDIS_KEY = "redis"


def attach_client(application: Application, settings: Settings) -> None:
    """Construct + stash the runtime singletons on ``application.bot_data``.

    Three keys land here:

    * ``BOT_DATA_KEY`` — the ``MerlinClient`` httpx pool.
    * ``BOT_DATA_ADMIN_KEY`` — frozen admin chat-id ``frozenset`` so
      handlers can do ``user.id in context.bot_data[...]`` without
      re-reading Settings on every update.
    * ``BOT_DATA_REDIS_KEY`` — ``redis.asyncio.Redis`` client backing the
      rate-limit middleware (separate Redis client from the one PTB's
      ``RedisPersistence`` owns so namespace + lifecycle are decoupled).
      The connection is lazy; ``from_url`` doesn't touch the network
      until the first command runs, so tests with no live Redis still
      construct the bot cleanly.

    Hot path; allocation-free; safe across the PTB worker pool.
    """
    client = MerlinClient(
        base_url=settings.merlin_api_base_url,
        token=settings.merlin_api_internal_token,
        timeout=settings.merlin_api_timeout_s,
    )
    application.bot_data[BOT_DATA_KEY] = client
    application.bot_data[BOT_DATA_ADMIN_KEY] = frozenset(settings.telegram_admin_chat_ids)
    application.bot_data[BOT_DATA_REDIS_KEY] = Redis.from_url(settings.redis_url)
