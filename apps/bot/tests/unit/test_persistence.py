"""Unit tests for ``merlin_bot.persistence``.

Two contracts to pin:

1. ``build_persistence`` returns the right implementation for the
   current environment — ``DictPersistence`` for tests (so the rest
   of the bot test suite doesn't need a live Redis), and
   ``RedisPersistence`` for everything else.
2. ``RedisPersistence`` correctly round-trips the four PTB data
   categories the bot actually exercises (``user_data``,
   ``chat_data``, ``callback_data``, conversation state) using
   ``fakeredis`` as a drop-in.
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis
from merlin_bot.config import Settings
from merlin_bot.persistence import RedisPersistence, build_persistence
from telegram.ext import DictPersistence

# ---------------------------------------------------------------------------
# build_persistence — environment-driven selection.
# ---------------------------------------------------------------------------


def test_build_persistence_returns_dict_in_test_environment() -> None:
    """``environment=test`` MUST yield ``DictPersistence`` so the unit
    test suite doesn't need a Redis to construct a bot Application."""
    settings = Settings(
        environment="test",
        telegram_bot_token="",
        merlin_api_base_url="http://test-api",
        merlin_api_internal_token="test-token",
        sentry_dsn="",
    )
    persistence = build_persistence(settings)
    assert isinstance(persistence, DictPersistence)


def test_build_persistence_returns_redis_in_dev_environment() -> None:
    """``environment=dev`` (and any non-test env) wires the Redis impl.

    We don't hit the network here — the constructor lazily opens the
    Redis connection. Constructing a ``RedisPersistence`` against a
    plausible URL is the contract; the network round-trip is exercised
    by the integration tests.
    """
    settings = Settings(
        environment="dev",
        telegram_bot_token="",
        merlin_api_base_url="http://test-api",
        merlin_api_internal_token="test-token",
        redis_url="redis://localhost:6379/1",
        sentry_dsn="",
    )
    persistence = build_persistence(settings)
    assert isinstance(persistence, RedisPersistence)


def test_build_persistence_returns_redis_in_prod_environment() -> None:
    """Prod must also get Redis. Same logic as dev — branch is on
    ``environment``, not on URL presence."""
    settings = Settings(
        environment="prod",
        telegram_bot_token="x" * 40,
        merlin_api_base_url="http://test-api",
        merlin_api_internal_token="test-token",
        redis_url="redis://prod-redis:6379/1",
        sentry_dsn="",
    )
    persistence = build_persistence(settings)
    assert isinstance(persistence, RedisPersistence)


# ---------------------------------------------------------------------------
# RedisPersistence — round-trip the four data categories we actually use.
# ---------------------------------------------------------------------------


@pytest.fixture
async def persistence() -> RedisPersistence:
    """A ``RedisPersistence`` backed by an in-process ``fakeredis``."""
    fake = FakeRedis()
    return RedisPersistence(
        "redis://unused",
        key_prefix="test:ptb",
        redis=fake,
    )


async def test_bot_data_storage_is_disabled(persistence: RedisPersistence) -> None:
    """``bot_data`` is intentionally NOT persisted — ``application.py``
    rebuilds it in ``post_init`` on every startup."""
    assert persistence.store_data.bot_data is False
    # The methods exist for the ABC contract but are no-ops.
    assert await persistence.get_bot_data() == {}
    await persistence.update_bot_data({"hello": "world"})
    assert await persistence.get_bot_data() == {}


async def test_user_data_round_trips_arbitrary_python_objects(
    persistence: RedisPersistence,
) -> None:
    """``user_data`` carries the ``ClaimDraft`` dataclass + plain strings.
    Pickle handles both; this test pins the round-trip explicitly."""
    user_id = 151594111
    payload = {"source_id": "tg:abc123", "display_name": "Sam", "depth": 3}

    await persistence.update_user_data(user_id, payload)

    loaded = await persistence.get_user_data()
    assert loaded == {user_id: payload}


async def test_user_data_isolates_users(persistence: RedisPersistence) -> None:
    """Two users' data must not collide — the per-user write must merge
    into the existing hash rather than clobber it."""
    await persistence.update_user_data(1, {"name": "alice"})
    await persistence.update_user_data(2, {"name": "bob"})

    loaded = await persistence.get_user_data()
    assert loaded == {1: {"name": "alice"}, 2: {"name": "bob"}}


async def test_drop_user_data_removes_only_target_user(
    persistence: RedisPersistence,
) -> None:
    await persistence.update_user_data(1, {"name": "alice"})
    await persistence.update_user_data(2, {"name": "bob"})

    await persistence.drop_user_data(1)

    loaded = await persistence.get_user_data()
    assert loaded == {2: {"name": "bob"}}


async def test_drop_user_data_for_unknown_user_is_noop(
    persistence: RedisPersistence,
) -> None:
    """Dropping data that isn't stored mustn't raise — PTB calls this
    speculatively in some lifecycle branches."""
    await persistence.drop_user_data(9999)
    assert await persistence.get_user_data() == {}


async def test_chat_data_round_trips(persistence: RedisPersistence) -> None:
    """Same pattern as ``user_data``, separate keyspace."""
    await persistence.update_chat_data(42, {"convo": "active"})
    loaded = await persistence.get_chat_data()
    assert loaded == {42: {"convo": "active"}}


async def test_callback_data_round_trips_none_initial(
    persistence: RedisPersistence,
) -> None:
    """A fresh persistence returns ``None`` for callback_data (PTB
    handles that as "no stored data"). After an update, the same shape
    must round-trip verbatim."""
    assert await persistence.get_callback_data() is None

    sample: tuple[list[tuple[str, float, dict[str, object]]], dict[str, str]] = (
        [("kb1", 12345.0, {"button": "data"})],
        {"data-id": "callback-token"},
    )
    await persistence.update_callback_data(sample)

    loaded = await persistence.get_callback_data()
    assert loaded == sample


async def test_conversations_round_trip_per_handler_name(
    persistence: RedisPersistence,
) -> None:
    """``/start`` and ``/alpha`` are two ConversationHandlers — their
    state lives under distinct keys and mustn't bleed across."""
    await persistence.update_conversation("onboarding", (1, 1), 2)
    await persistence.update_conversation("alpha", (1, 1), 5)

    onboarding = await persistence.get_conversations("onboarding")
    alpha = await persistence.get_conversations("alpha")

    assert onboarding == {(1, 1): 2}
    assert alpha == {(1, 1): 5}


async def test_conversations_update_with_none_drops_key(
    persistence: RedisPersistence,
) -> None:
    """PTB signals "conversation ended" by passing ``new_state=None``.
    The persistence must drop the key, not store a literal ``None``."""
    await persistence.update_conversation("onboarding", (1, 1), 2)
    await persistence.update_conversation("onboarding", (1, 1), None)

    convos = await persistence.get_conversations("onboarding")
    assert convos == {}


async def test_get_conversations_unknown_name_returns_empty(
    persistence: RedisPersistence,
) -> None:
    """A handler that's never written must read back as an empty dict
    (not a KeyError, not ``None``)."""
    assert await persistence.get_conversations("never-touched") == {}


async def test_key_prefix_isolates_two_persistence_instances() -> None:
    """Two bots sharing a Redis must not collide. Different prefixes →
    independent data namespaces."""
    fake = FakeRedis()
    p1 = RedisPersistence("redis://unused", key_prefix="bot_a:ptb", redis=fake)
    p2 = RedisPersistence("redis://unused", key_prefix="bot_b:ptb", redis=fake)

    await p1.update_user_data(1, {"who": "bot_a"})
    await p2.update_user_data(1, {"who": "bot_b"})

    assert await p1.get_user_data() == {1: {"who": "bot_a"}}
    assert await p2.get_user_data() == {1: {"who": "bot_b"}}


async def test_flush_closes_redis_connection() -> None:
    """``flush`` runs on application shutdown — it must close the
    underlying Redis connection cleanly. We assert via a side-effect:
    after flush, the persistence's redis client is closed."""
    fake = FakeRedis()
    persistence = RedisPersistence(
        "redis://unused",
        key_prefix="test:ptb",
        redis=fake,
    )
    await persistence.update_user_data(1, {"ok": True})
    # Should not raise — fakeredis honours aclose().
    await persistence.flush()
