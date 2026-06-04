"""Unit tests for ``merlin_bot.middleware.rate_limit``.

Contracts pinned here:

1. Trigger detection — which updates count as ``/alpha`` LLM-triggers
   and which intentionally don't (we don't throttle ``/board``,
   ``/me``, callback buttons, or in-flow text edits).
2. Per-call cooldown — two ``/alpha`` calls within
   ``ALPHA_COOLDOWN_SECONDS`` from the same user → the second one is
   blocked (``ApplicationHandlerStop`` raised) and gets a polite reply.
3. Per-user isolation — one spammy user does not throttle another.
4. Hourly burst cap — over ``ALPHA_BURST_PER_HOUR`` calls inside an
   hour → blocked. Below the cap → passes.
5. Fail-open — a missing Redis client doesn't break the bot; the
   update flows through.

Tests use ``fakeredis.aioredis.FakeRedis`` (already a bot dev dep for
``RedisPersistence``) as a drop-in for the production async Redis
client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis.aioredis import FakeRedis
from merlin_bot.api_dependency import BOT_DATA_REDIS_KEY
from merlin_bot.handlers.alpha.states import USER_KEY_DRAFT
from merlin_bot.middleware.rate_limit import (
    ALPHA_BURST_PER_HOUR,
    _is_alpha_command,
    _is_alpha_trigger,
    alpha_cooldown,
    build_handler,
)
from telegram import Update
from telegram.ext import ApplicationHandlerStop, TypeHandler

# ---------------------------------------------------------------------------
# Trigger detection — pure functions, no Redis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/alpha",
        "/alpha ",
        "/alpha eth long",
        "/alpha@MerlinBot",
        "/alpha@MerlinBot eth long",
    ],
)
def test_is_alpha_command_matches_command_variants(text: str) -> None:
    assert _is_alpha_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "/alphas",
        "/alphabet",
        "/board",
        "alpha",
        "hello /alpha",
        "",
    ],
)
def test_is_alpha_command_rejects_non_command(text: str) -> None:
    assert not _is_alpha_command(text)


def _make_update(text: str | None, user_id: int = 1) -> Update:
    """Build a minimal Update fixture for the trigger heuristic.

    The middleware reads exactly three fields off the Update —
    ``effective_user.id``, ``message``, ``message.text`` — so the
    MagicMock surface is intentionally tiny.
    """
    message = MagicMock(spec=["text", "reply_text"]) if text is not None else None
    if message is not None:
        message.text = text
        message.reply_text = AsyncMock()
    user = MagicMock(spec=["id"])
    user.id = user_id
    update = MagicMock(spec=["message", "effective_user"])
    update.message = message
    update.effective_user = user
    return update  # type: ignore[no-any-return]


def _make_context(
    *,
    redis_client: Any | None = None,
    user_data: dict[Any, Any] | None = None,
) -> Any:
    """Build a minimal context with ``bot_data`` + ``user_data`` slots.

    Matches the shape PTB hands to handlers; the middleware only reads
    ``context.bot_data[BOT_DATA_REDIS_KEY]`` and ``context.user_data``.
    """
    context = MagicMock(spec=["bot_data", "user_data"])
    context.bot_data = {BOT_DATA_REDIS_KEY: redis_client} if redis_client is not None else {}
    context.user_data = user_data if user_data is not None else {}
    return context


def test_is_alpha_trigger_detects_slash_alpha() -> None:
    """``/alpha`` is always a trigger, even with no active draft."""
    update = _make_update("/alpha")
    context = _make_context()
    assert _is_alpha_trigger(update, context)


def test_is_alpha_trigger_detects_slash_alpha_with_args() -> None:
    update = _make_update("/alpha eth long 2 weeks")
    context = _make_context()
    assert _is_alpha_trigger(update, context)


def test_is_alpha_trigger_detects_freeform_text_when_idle() -> None:
    """Plain text from an idle user (no draft) → freeform catch-all entry."""
    update = _make_update("eth pumping next week")
    context = _make_context()  # no USER_KEY_DRAFT
    assert _is_alpha_trigger(update, context)


def test_is_alpha_trigger_skips_freeform_text_when_draft_active() -> None:
    """Plain text WITH an active draft is an in-flow edit (EDIT_THESIS,
    EDIT_HORIZON typed input, etc.) — not an LLM trigger."""
    update = _make_update("change my mind, this is now a short")
    context = _make_context(user_data={USER_KEY_DRAFT: {"thesis_text": "..."}})
    assert not _is_alpha_trigger(update, context)


@pytest.mark.parametrize("cmd", ["/board", "/me", "/help", "/history"])
def test_is_alpha_trigger_skips_other_commands(cmd: str) -> None:
    """Non-``/alpha`` commands don't touch upstream paid APIs."""
    update = _make_update(cmd)
    context = _make_context()
    assert not _is_alpha_trigger(update, context)


def test_is_alpha_trigger_skips_empty_text() -> None:
    update = _make_update("   ")
    context = _make_context()
    assert not _is_alpha_trigger(update, context)


def test_is_alpha_trigger_skips_non_message_updates() -> None:
    """Callback queries, edited messages, etc. have ``message=None``."""
    update = _make_update(None)
    context = _make_context()
    assert not _is_alpha_trigger(update, context)


# ---------------------------------------------------------------------------
# Cooldown + hourly behaviour — backed by fakeredis
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client() -> FakeRedis:
    return FakeRedis()


async def test_first_alpha_call_passes(redis_client: FakeRedis) -> None:
    """A user's first ``/alpha`` ever must NOT be throttled — the
    cooldown key doesn't exist yet, the SET-NX acquires it, and the
    handler returns without raising."""
    update = _make_update("/alpha")
    context = _make_context(redis_client=redis_client)
    # No exception → passes through.
    await alpha_cooldown(update, context)


async def test_second_alpha_call_within_cooldown_is_blocked(
    redis_client: FakeRedis,
) -> None:
    """Two ``/alpha`` calls back-to-back from the same user → the
    second raises ``ApplicationHandlerStop`` and the user gets the
    cooldown reply. Pins the cost-protection contract."""
    update_1 = _make_update("/alpha", user_id=42)
    update_2 = _make_update("/alpha", user_id=42)
    context = _make_context(redis_client=redis_client)

    await alpha_cooldown(update_1, context)

    with pytest.raises(ApplicationHandlerStop):
        await alpha_cooldown(update_2, context)

    update_2.message.reply_text.assert_awaited_once()
    reply_text = update_2.message.reply_text.await_args.args[0]
    assert "easy" in reply_text.lower() or "wait" in reply_text.lower()


async def test_cooldown_isolates_users(redis_client: FakeRedis) -> None:
    """User A's cooldown must NOT throttle User B — the key includes
    the per-user id."""
    a = _make_update("/alpha", user_id=1)
    b = _make_update("/alpha", user_id=2)
    context = _make_context(redis_client=redis_client)

    await alpha_cooldown(a, context)
    # User B's first call must pass even though User A just consumed
    # their slot.
    await alpha_cooldown(b, context)
    b.message.reply_text.assert_not_called()


async def test_freeform_text_uses_same_cooldown(redis_client: FakeRedis) -> None:
    """The freeform catch-all path (plain text from an idle user) must
    consume the same cooldown bucket as ``/alpha`` — both fire
    ``draft_claim`` so they must share a limit."""
    cmd = _make_update("/alpha", user_id=7)
    freeform = _make_update("eth long next week", user_id=7)
    context = _make_context(redis_client=redis_client)

    await alpha_cooldown(cmd, context)
    with pytest.raises(ApplicationHandlerStop):
        await alpha_cooldown(freeform, context)


async def test_non_alpha_command_is_not_throttled(redis_client: FakeRedis) -> None:
    """``/board`` must always pass; it doesn't touch upstream APIs."""
    update = _make_update("/board", user_id=9)
    context = _make_context(redis_client=redis_client)
    await alpha_cooldown(update, context)
    update.message.reply_text.assert_not_called()


async def test_text_during_active_draft_is_not_throttled(
    redis_client: FakeRedis,
) -> None:
    """In-flow text edits (EDIT_THESIS, EDIT_HORIZON freeform) carry
    ``USER_KEY_DRAFT`` in ``user_data`` and must pass — those don't
    hit OpenRouter."""
    update = _make_update("changed my mind, 3 days", user_id=11)
    context = _make_context(
        redis_client=redis_client,
        user_data={USER_KEY_DRAFT: {"thesis_text": "..."}},
    )
    await alpha_cooldown(update, context)
    update.message.reply_text.assert_not_called()


async def test_hourly_burst_cap_blocks_after_threshold(
    redis_client: FakeRedis,
) -> None:
    """Above ``ALPHA_BURST_PER_HOUR`` calls in an hour → blocked.

    To test this without sleeping, we pre-seed the hourly counter at
    the cap and then submit one more call. The per-call cooldown key
    is also pre-aged so the SET-NX succeeds and we land on the
    INCR + cap check.
    """
    user_id = 99
    hourly_key = f"merlin_bot:rl:alpha:hr:{user_id}"
    # Seed the hourly counter at the cap; the next INCR pushes it over.
    await redis_client.set(hourly_key, ALPHA_BURST_PER_HOUR)
    await redis_client.expire(hourly_key, 3600)

    update = _make_update("/alpha", user_id=user_id)
    context = _make_context(redis_client=redis_client)
    with pytest.raises(ApplicationHandlerStop):
        await alpha_cooldown(update, context)
    reply = update.message.reply_text.await_args.args[0]
    assert "hour" in reply.lower() or "cap" in reply.lower()


async def test_hourly_counter_is_initialised_with_expiry(
    redis_client: FakeRedis,
) -> None:
    """First call from a user → INCR makes the key, EXPIRE sets the
    1-hour TTL. Without the EXPIRE the bucket would persist forever
    and a long-running session would never reset."""
    user_id = 77
    update = _make_update("/alpha", user_id=user_id)
    context = _make_context(redis_client=redis_client)
    await alpha_cooldown(update, context)

    hourly_key = f"merlin_bot:rl:alpha:hr:{user_id}"
    ttl = await redis_client.ttl(hourly_key)
    # fakeredis returns the TTL in seconds; we expect roughly 3600.
    assert 0 < ttl <= 3600


# ---------------------------------------------------------------------------
# Fail-open behaviour
# ---------------------------------------------------------------------------


async def test_missing_redis_client_passes_through() -> None:
    """No Redis on ``bot_data`` (e.g. test harness without one)
    → middleware logs a warning and returns; the update is NOT blocked."""
    update = _make_update("/alpha")
    context = _make_context()  # no redis attached
    # Must not raise.
    await alpha_cooldown(update, context)
    update.message.reply_text.assert_not_called()


async def test_redis_set_error_passes_through() -> None:
    """A flaky Redis must not break the bot. SET failing → log + pass."""

    class _ExplodingRedis:
        async def set(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("redis is on fire")

        async def incr(self, *_a: object, **_kw: object) -> int:
            raise AssertionError("incr must not be reached after set failure")

    update = _make_update("/alpha")
    context = _make_context(redis_client=_ExplodingRedis())
    await alpha_cooldown(update, context)
    update.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# build_handler — wiring contract
# ---------------------------------------------------------------------------


def test_build_handler_returns_type_handler() -> None:
    """The handler factory returns a ``TypeHandler`` matched against
    every ``Update`` — the broadest filter so the limiter is
    evaluated before any business handler."""
    handler = build_handler()
    assert isinstance(handler, TypeHandler)
    assert handler.callback is alpha_cooldown
