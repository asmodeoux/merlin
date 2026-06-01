"""Unit tests for the admin-only ``/revoke <code>`` command.

The handler checks the admin allowlist, parses the single ``code``
argument, calls ``MerlinClient.revoke_invite``, and replies with a
MarkdownV2 confirmation. Specific failure surfaces:

* Non-admin caller → generic "unknown command" reply (operator surface
  stays hidden).
* Missing / extra args → usage hint.
* 404 from the API → "already consumed" copy.
* Any other API error → recoverable "try again" message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY
from merlin_bot.handlers.admin.revoke_command import revoke_command

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
    args: list[str] | None = None


async def test_revoke_admin_kills_code_and_replies() -> None:
    client = AsyncMock()
    client.revoke_invite.return_value = {
        "code": "abc123XYZ",
        "revoked_at": "2026-06-01T12:00:00+00:00",
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
        args=["abc123XYZ"],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    client.revoke_invite.assert_awaited_once_with(
        code="abc123XYZ",
        tg_user_id=ADMIN_ID,
    )
    assert update.message is not None
    body, kwargs = update.message.replies[0]
    assert "`abc123XYZ`" in body
    assert "revoked" in body.lower()
    assert kwargs.get("parse_mode") == "MarkdownV2"


async def test_revoke_non_admin_gets_unknown_command_reply() -> None:
    """Operator surface stays hidden — same shape as ``/invite``."""
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
        args=["abc123XYZ"],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    client.revoke_invite.assert_not_called()
    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "admin" not in body.lower()
    assert "/help" in body


async def test_revoke_without_args_shows_usage() -> None:
    """``/revoke`` with no arg → usage hint; API is not called."""
    client = AsyncMock()
    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
        args=[],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    client.revoke_invite.assert_not_called()
    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "usage" in body.lower()
    assert "<code>" in body.lower()


async def test_revoke_with_too_many_args_shows_usage() -> None:
    """Multiple tokens isn't supported (yet) — show usage instead of
    silently revoking the first one."""
    client = AsyncMock()
    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
        args=["one", "two"],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    client.revoke_invite.assert_not_called()
    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "usage" in body.lower()


async def test_revoke_404_replies_already_consumed() -> None:
    """The route 404s if the code is unknown / redeemed / revoked. The
    bot collapses those to a single "already consumed" message because
    none of them are actionable for the admin."""
    from merlin_client.errors import MerlinHTTPError

    client = AsyncMock()
    client.revoke_invite.side_effect = MerlinHTTPError(404, "not found")

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
        args=["GHOST_CODE"],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "consumed" in body.lower() or "doesn't exist" in body.lower()


async def test_revoke_other_api_error_replies_temporary() -> None:
    """5xx and similar map to a recoverable "try again" copy."""
    from merlin_client.errors import MerlinHTTPError

    client = AsyncMock()
    client.revoke_invite.side_effect = MerlinHTTPError(503, "unavailable")

    update = _FakeUpdate(
        effective_user=_FakeUser(id=ADMIN_ID, username="operator"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(
        bot_data={
            BOT_DATA_KEY: client,
            BOT_DATA_ADMIN_KEY: frozenset({ADMIN_ID}),
        },
        args=["abc123XYZ"],
    )

    await revoke_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try again" in body.lower()
