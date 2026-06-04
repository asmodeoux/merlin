"""Tiny Redis-backed response cache for upstream-quota-touching routes.

Threat model: legitimate-but-spammy users burning CoinGecko's free-tier
rate limit. Two endpoints touch CoinGecko:

* ``GET /assets/search?q=`` — pure CoinGecko ``/search`` fanout. Same
  ``q`` returns identical results across users; ideal cross-user cache.
* ``POST /assets/resolve`` — CoinGecko ``/coins/{id}``. Idempotent per
  ``coingecko_id``; metadata changes rarely.

Design choices:

* Hand-rolled (no new dep). The cache surface is ~10 lines of Redis
  ``GET``/``SETEX`` per endpoint; ``fastapi-cache2`` would add a
  middleware + key-builder DSL we don't need.
* JSON-serialised (not pickle). Forward-compatible with future bot
  versions; doesn't trip the ``ruff S301`` pickle warning the rest of
  the repo already navigates.
* Two sentinels distinguish three states: cache miss (key absent),
  positive hit (JSON payload), negative hit (the literal ``_NEG``
  byte). Negative caching is important — a typo'd query like
  ``"asdfghjkl"`` would otherwise re-hit CoinGecko every time someone
  retried it; the 30s sentinel breaks that loop without making real
  recovery (after a CoinGecko outage) wait 5 minutes.
* Fail-open everywhere. If Redis is down or returns garbage, the
  upstream call still happens and the user gets a fresh result; only
  the deduplication benefit is lost.

All TTLs are code constants, not env vars — they don't differ between
environments. Change them in the same PR that ships the deploy.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = logging.getLogger("merlin.api.cache")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

ASSETS_SEARCH_TTL_SECONDS = 300
"""5 min cache on ``/assets/search`` results.

CoinGecko's ranked-search output churns slowly; 5 min is short enough
that a newly-listed coin appears in the picker within a single Telegram
session for any active user, and long enough that 20 users typing the
same trending ticker dedupe into one upstream call.
"""

ASSETS_RESOLVE_TTL_SECONDS = 3600
"""1 hr cache on ``/assets/resolve`` metadata.

The bot calls this on Confirm to validate the coingecko_id and grab
symbol + name. Both fields are effectively immutable; 1 hr is a
generous lower bound that still lets a manual cache-bust (FLUSHDB) act
as a hot fix without redeploy.
"""

NEGATIVE_CACHE_TTL_SECONDS = 30
"""Short sentinel cache on "no result" so retries don't re-hit upstream.

A typo'd or junk query produces a quick miss-then-miss feedback loop
when a user re-types. 30 s is short enough to recover from a genuine
CoinGecko outage without user-visible staleness, long enough to break
a doom-loop of repeated identical retries.
"""

REDIS_PREFIX = "merlin_api:cache"
"""Namespace for every cache key. Distinct from the RQ queue and from
any future merlin_api Redis usage so a ``FLUSHDB`` on the cache can
target keys precisely.
"""

# Sentinel value stored when an upstream call returned "no result"
# (e.g. AssetNotFoundError from /assets/resolve). One byte to keep the
# negative-cache row cheap.
_NEG_SENTINEL = b"\x00"

# Module-level so callers can branch on it explicitly without importing
# its byte representation.
NEGATIVE_CACHE_MISS = "negative-cache-miss"


def make_key(namespace: str, *parts: str) -> str:
    """Build a cache key under ``REDIS_PREFIX:{namespace}:{joined}``.

    Keys are joined by ``:`` and trimmed/lowercased for the search
    namespace at the call site; this function stays neutral.
    """
    return ":".join((REDIS_PREFIX, namespace, *parts))


async def cached_or_compute[T: BaseModel](
    redis: Redis | None,
    *,
    key: str,
    ttl: int,
    model: type[T],
    loader: Callable[[], Awaitable[T]],
    negative_loader_exception: type[Exception] | None = None,
    negative_ttl: int = NEGATIVE_CACHE_TTL_SECONDS,
) -> T:
    """Look up ``key`` in Redis; on miss, call ``loader`` and cache.

    * Positive hit → JSON-decode into ``model`` and return.
    * Negative hit (the ``_NEG_SENTINEL``) → raise the same
      ``negative_loader_exception`` the loader would have raised.
    * Miss → call ``loader``; on success cache the JSON dump for
      ``ttl`` seconds; if it raises ``negative_loader_exception``,
      cache the sentinel for ``negative_ttl`` seconds and re-raise.

    Fail-open: any Redis error short-circuits to the loader. The
    upstream still gets hit; only deduplication is lost.
    """
    if redis is None:
        return await loader()

    try:
        blob = await redis.get(key)
    except Exception as e:
        _log.warning("cache GET failed (%s); falling through to upstream", e)
        return await loader()

    if blob == _NEG_SENTINEL:
        if negative_loader_exception is None:
            # Caller didn't opt into negative caching; treat as miss.
            return await loader()
        raise negative_loader_exception(NEGATIVE_CACHE_MISS)

    if blob is not None:
        try:
            return model.model_validate_json(blob)
        except Exception as e:
            _log.warning("cache hit but JSON decode failed (%s); refetching", e)

    try:
        value = await loader()
    except Exception as e:
        if negative_loader_exception is not None and isinstance(
            e,
            negative_loader_exception,
        ):
            try:
                await redis.setex(key, negative_ttl, _NEG_SENTINEL)
            except Exception as cache_err:
                _log.warning("cache SETEX (negative) failed: %s", cache_err)
        raise

    try:
        await redis.setex(key, ttl, value.model_dump_json().encode("utf-8"))
    except Exception as e:
        _log.warning("cache SETEX failed (%s); result not cached", e)
    return value
