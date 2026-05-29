"""Unit tests for the admin-only ``/invite`` command.

The command checks the admin allowlist on every call, calls
``MerlinClient.create_invite``, and replies with the freshly-minted
code formatted as a copyable MarkdownV2 code block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY
from merlin_bot.handlers.admin.invite_command import invite_command

ADMIN_ID = 151594111
NON_ADMIN_ID = 222


@dataclass
class _FakeUser:
    id: int
    username: str | None = None


@dataclass
class _FakeMessage:
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append((text, kw))


@dataclass
class _FakeUpdate:
    effective_user: _FakeUser | None
    message: _FakeMessage | None


@dataclass
class _FakeContext:
    bot_data: dict[str, Any]


async def test_invite_admin_mints_code_and_replies() -> None:
    client = AsyncMock()
    client.create_invite.return_value = {
        "code": "abc123XYZ",
        "expires_at": "2026-06-12T12:00:00+00:00",
    }

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )
    await invite_command(update, ctx)  # type: ignore[arg-type]

    client.create_invite.assert_awaited_once_with(tg_user_id=ADMIN_ID, expires_in_days=14)
    assert update.message is not None
    assert len(update.message.replies) == 1
    body, kwargs = update.message.replies[0]
    # MarkdownV2 wrapped — the code is in a backtick block.
    assert "`abc123XYZ`" in body
    assert kwargs.get("parse_mode") == "MarkdownV2"


async def test_invite_non_admin_gets_unknown_command_reply() -> None:
    """Non-admins must NOT see "you aren't an admin" — that would
    confirm the operator surface exists. The reply mirrors Telegram's
    generic "unknown command" tone, and ``create_invite`` is not called."""
    client = AsyncMock()

    update = _FakeUpdate(
        effective_user=_FakeUser(id=NON_ADMIN_ID, username="newcomer"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )
    await invite_command(update, ctx)  # type: ignore[arg-type]

    client.create_invite.assert_not_called()
    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "admin" not in body.lower()
    assert "/help" in body


async def test_invite_api_failure_replies_temporary_error() -> None:
    from merlin_client.errors import MerlinHTTPError

    client = AsyncMock()
    client.create_invite.side_effect = MerlinHTTPError(503, "unavailable")

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
    )
    await invite_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try again" in body.lower()
