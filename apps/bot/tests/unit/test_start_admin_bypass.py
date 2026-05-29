"""Admin bypass in ``/start`` — Sprint 1 onboarding without an invite code.

When a Telegram user whose id is in ``Settings.telegram_admin_chat_ids``
runs ``/start``, the bot skips the invite-code prompt and calls
``POST /sources/admin_bootstrap`` instead. These tests pin that branch
of the conversation flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY
from merlin_bot.handlers.start import StartState, start_command
from telegram.ext import ConversationHandler

ADMIN_ID = 151594111
NON_ADMIN_ID = 222


# ---------------------------------------------------------------------------
# Tiny fakes — just enough surface for the handler.
# ---------------------------------------------------------------------------


@dataclass
class _FakeUser:
    id: int
    username: str | None = None


@dataclass
class _FakeMessage:
    replies: list[str] = field(default_factory=list)

    async def reply_text(self, text: str, **_kw: Any) -> None:
        self.replies.append(text)


@dataclass
class _FakeUpdate:
    effective_user: _FakeUser | None
    message: _FakeMessage | None


@dataclass
class _FakeContext:
    user_data: dict[str, Any] | None
    bot_data: dict[str, Any]


# ---------------------------------------------------------------------------
# Admin bypass — happy path.
# ---------------------------------------------------------------------------


async def test_admin_start_skips_invite_and_calls_bootstrap() -> None:
    """Admin's first ``/start`` MUST skip the invite prompt and call
    ``admin_bootstrap``, then prompt for display name."""
    client = AsyncMock()
    client.admin_bootstrap.return_value = {
        "source_id": "tg:abc123",
        "display_name_prompt": True,
    }

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        user_data={},
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )

    state = await start_command(update, ctx)  # type: ignore[arg-type]

    client.admin_bootstrap.assert_awaited_once_with(
        tg_user_id=ADMIN_ID,
        tg_handle="operator",
    )
    assert state == StartState.AWAIT_DISPLAY_NAME
    assert ctx.user_data is not None
    assert ctx.user_data["source_id"] == "tg:abc123"
    # The reply must be the admin-flavoured welcome, NOT the "paste your code" prompt.
    assert update.message is not None
    assert not any(
        "paste" in r.lower() for r in update.message.replies
    ), "admin must NOT be asked to paste an invite code"
    # The admin welcome mentions /invite (the command they can use).
    assert any("/invite" in r for r in update.message.replies)


async def test_admin_start_short_circuits_if_display_name_set() -> None:
    """Idempotent re-bootstrap (display_name already set) → END."""
    client = AsyncMock()
    client.admin_bootstrap.return_value = {
        "source_id": "tg:abc123",
        "display_name_prompt": False,
    }

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        user_data={},
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )

    state = await start_command(update, ctx)  # type: ignore[arg-type]
    assert state == ConversationHandler.END


# ---------------------------------------------------------------------------
# Non-admin — must still hit the invite prompt unchanged.
# ---------------------------------------------------------------------------


async def test_non_admin_start_goes_through_invite_flow() -> None:
    client = AsyncMock()
    update = _FakeUpdate(
        effective_user=_FakeUser(id=NON_ADMIN_ID, username="newcomer"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        user_data={},
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )
    state = await start_command(update, ctx)  # type: ignore[arg-type]
    assert state == StartState.AWAIT_INVITE
    client.admin_bootstrap.assert_not_called()
    assert update.message is not None
    assert any("paste" in r.lower() for r in update.message.replies)


# ---------------------------------------------------------------------------
# Bootstrap failure — graceful degradation to the invite flow.
# ---------------------------------------------------------------------------


async def test_admin_bootstrap_failure_falls_back_to_invite_flow() -> None:
    from merlin_client.errors import MerlinHTTPError

    client = AsyncMock()
    client.admin_bootstrap.side_effect = MerlinHTTPError(500, "internal")

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        user_data={},
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )
    state = await start_command(update, ctx)  # type: ignore[arg-type]
    assert state == StartState.AWAIT_INVITE
    # The user still sees the invite prompt — the admin can still
    # onboard via a real invite code even if bootstrap is broken.
    assert update.message is not None
    assert any("paste" in r.lower() for r in update.message.replies)
