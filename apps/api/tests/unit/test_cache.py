"""Unit tests for ``merlin_api.cache.cached_or_compute``.

The contracts pinned here are independent of any route — they're the
generic behaviour the helper promises to every caller:

1. Cache miss → loader runs → result is JSON-serialised and stored
   with the requested TTL.
2. Cache hit → loader is NOT invoked; the cached value round-trips
   through ``model.model_validate_json``.
3. Negative cache: when a loader raises a registered exception, a
   short-TTL sentinel is stored; subsequent calls re-raise without
   invoking the loader.
4. Fail-open on Redis errors — a flaky Redis must not break the
   caller; the loader still runs and its result is returned.

Tests use ``fakeredis.aioredis.FakeRedis`` (already an API dev dep).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from merlin_api.cache import (
    NEGATIVE_CACHE_TTL_SECONDS,
    cached_or_compute,
    make_key,
)
from pydantic import BaseModel


class _Echo(BaseModel):
    """Trivial Pydantic model used as the cached payload."""

    value: str


class _Missing(Exception):
    """Test stand-in for ``AssetNotFoundError``."""


@pytest.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    fake = FakeRedis()
    try:
        yield fake
    finally:
        await fake.aclose()


# ---------------------------------------------------------------------------
# make_key — small utility, tested separately so the rest of the suite
# can rely on the key shape without re-deriving it.
# ---------------------------------------------------------------------------


def test_make_key_joins_namespace_and_parts() -> None:
    assert make_key("foo", "bar", "baz") == "merlin_api:cache:foo:bar:baz"


def test_make_key_with_single_part() -> None:
    assert make_key("foo", "bar") == "merlin_api:cache:foo:bar"


# ---------------------------------------------------------------------------
# Miss → load → cache
# ---------------------------------------------------------------------------


async def test_cache_miss_invokes_loader_and_caches(redis_client: FakeRedis) -> None:
    loader = AsyncMock(return_value=_Echo(value="fresh"))
    result = await cached_or_compute(
        redis_client,
        key="merlin_api:cache:test:miss",
        ttl=60,
        model=_Echo,
        loader=loader,
    )
    assert result.value == "fresh"
    loader.assert_awaited_once()
    # The key now exists with a TTL roughly equal to what we asked for.
    ttl = await redis_client.ttl("merlin_api:cache:test:miss")
    assert 0 < ttl <= 60


async def test_cache_hit_does_not_invoke_loader(redis_client: FakeRedis) -> None:
    """The second call with the same key must return the cached value
    and skip the loader entirely — this is the only thing the cache
    exists to do."""
    loader_1 = AsyncMock(return_value=_Echo(value="first"))
    loader_2 = AsyncMock(return_value=_Echo(value="should-not-run"))

    r1 = await cached_or_compute(
        redis_client,
        key="merlin_api:cache:test:hit",
        ttl=60,
        model=_Echo,
        loader=loader_1,
    )
    r2 = await cached_or_compute(
        redis_client,
        key="merlin_api:cache:test:hit",
        ttl=60,
        model=_Echo,
        loader=loader_2,
    )
    assert r1.value == "first"
    assert r2.value == "first", "second call must come from cache, not loader_2"
    loader_2.assert_not_awaited()


async def test_cache_falls_through_when_redis_is_none() -> None:
    """``redis=None`` (e.g. test harness without a client) → loader
    is always invoked; nothing is cached. Fail-open."""
    loader = AsyncMock(return_value=_Echo(value="bare"))
    result = await cached_or_compute(
        None,
        key="merlin_api:cache:test:none",
        ttl=60,
        model=_Echo,
        loader=loader,
    )
    assert result.value == "bare"
    loader.assert_awaited_once()


# ---------------------------------------------------------------------------
# Negative caching
# ---------------------------------------------------------------------------


async def test_negative_cache_short_circuits_repeat_load(redis_client: FakeRedis) -> None:
    """When the loader raises the registered exception, the sentinel
    is stored under the cache key. Subsequent calls re-raise without
    invoking the loader."""
    loader = AsyncMock(side_effect=_Missing("not found"))

    with pytest.raises(_Missing):
        await cached_or_compute(
            redis_client,
            key="merlin_api:cache:test:neg",
            ttl=300,
            model=_Echo,
            loader=loader,
            negative_loader_exception=_Missing,
        )
    assert loader.await_count == 1

    # Second call → cache should return the negative sentinel without
    # calling loader.
    with pytest.raises(_Missing):
        await cached_or_compute(
            redis_client,
            key="merlin_api:cache:test:neg",
            ttl=300,
            model=_Echo,
            loader=loader,
            negative_loader_exception=_Missing,
        )
    assert loader.await_count == 1, "negative cache must short-circuit the loader"


async def test_negative_cache_uses_short_ttl(redis_client: FakeRedis) -> None:
    """A negative sentinel must be stored with the short TTL
    (``NEGATIVE_CACHE_TTL_SECONDS``), not the long positive one — so
    a real recovery is visible within seconds."""
    loader = AsyncMock(side_effect=_Missing("not found"))
    with pytest.raises(_Missing):
        await cached_or_compute(
            redis_client,
            key="merlin_api:cache:test:neg-ttl",
            ttl=3600,
            model=_Echo,
            loader=loader,
            negative_loader_exception=_Missing,
        )
    ttl = await redis_client.ttl("merlin_api:cache:test:neg-ttl")
    assert 0 < ttl <= NEGATIVE_CACHE_TTL_SECONDS


async def test_non_registered_exception_is_not_negative_cached(
    redis_client: FakeRedis,
) -> None:
    """Only the exception class passed as ``negative_loader_exception``
    is cached as a miss. Other exceptions (e.g. transport errors)
    bubble out unchanged — the next caller hits upstream fresh."""

    class _OtherFailure(Exception):
        pass

    loader = AsyncMock(side_effect=_OtherFailure("transport"))
    with pytest.raises(_OtherFailure):
        await cached_or_compute(
            redis_client,
            key="merlin_api:cache:test:transient",
            ttl=300,
            model=_Echo,
            loader=loader,
            negative_loader_exception=_Missing,
        )

    # The key must NOT exist — caching a transient failure would
    # convert a 1-second outage into a 5-minute one.
    assert await redis_client.get("merlin_api:cache:test:transient") is None


# ---------------------------------------------------------------------------
# Fail-open on Redis errors
# ---------------------------------------------------------------------------


async def test_redis_get_failure_passes_through_to_loader() -> None:
    """A flaky Redis (or one mid-restart) must not block the request.
    GET raising → loader runs and the user gets a fresh response."""

    class _ExplodingRedis:
        async def get(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("redis on fire")

        async def setex(self, *_a: object, **_kw: object) -> None:
            raise AssertionError("setex must not run after GET failure")

    loader = AsyncMock(return_value=_Echo(value="recovered"))
    result = await cached_or_compute(
        _ExplodingRedis(),  # type: ignore[arg-type]
        key="merlin_api:cache:test:get-fail",
        ttl=60,
        model=_Echo,
        loader=loader,
    )
    assert result.value == "recovered"
    loader.assert_awaited_once()


async def test_redis_setex_failure_returns_loader_result() -> None:
    """SETEX failing must NOT propagate — the loader value is
    returned to the caller regardless. Only the deduplication
    benefit is lost on the next call."""

    class _PartialRedis:
        async def get(self, *_a: object, **_kw: object) -> None:
            return None

        async def setex(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("redis write failed")

    loader = AsyncMock(return_value=_Echo(value="ok"))
    result = await cached_or_compute(
        _PartialRedis(),  # type: ignore[arg-type]
        key="merlin_api:cache:test:setex-fail",
        ttl=60,
        model=_Echo,
        loader=loader,
    )
    assert result.value == "ok"


async def test_corrupt_cache_value_falls_through_to_loader(
    redis_client: FakeRedis,
) -> None:
    """If the stored bytes can't be JSON-decoded into the requested
    model (e.g. someone wrote garbage, or the schema changed in
    place), we treat it as a miss and refetch. The user never sees
    a 500 from a stale-shape cache row."""
    key = "merlin_api:cache:test:corrupt"
    await redis_client.set(key, b"not-json-{")
    loader = AsyncMock(return_value=_Echo(value="fallback"))
    result = await cached_or_compute(
        redis_client,
        key=key,
        ttl=60,
        model=_Echo,
        loader=loader,
    )
    assert result.value == "fallback"
    loader.assert_awaited_once()
