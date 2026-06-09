"""Unit tests for the (open-to-all-onboarded-sources) ``/invite`` command.

``/invite`` used to be admin-gated client-side; the bot would silently
short-circuit non-admins with an "unknown command" reply. That gate
moved to the API in the open-invites rollout — every onboarded source
can now mint codes, and the bot's only job is to call the API and map
its responses to user-facing copy.

The contract these tests pin:

* Happy path — caller is onboarded → API returns ``{code, expires_at}``
  → bot replies with a MarkdownV2 code block.
* API 403 (``not_a_source``) — caller typed ``/invite`` before
  finishing ``/start`` → bot replies with a friendly "send /start
  first" nudge, NOT the generic retry message.
* Other API errors — transient outage → bot replies with the recoverable
  "try again in a moment" copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.invite_command import invite_command
from merlin_client.errors import MerlinHTTPError

CALLER_ID = 222333444


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


def _build_update_and_context(
    *,
    client: AsyncMock,
    tg_user_id: int = CALLER_ID,
) -> tuple[_FakeUpdate, _FakeContext]:
    """Compose the (update, context) pair every test needs.

    The bot_data only carries the API client now — the
    ``BOT_DATA_ADMIN_KEY`` allowlist used to gate this handler is
    gone, so we deliberately omit it to assert the handler doesn't
    secretly still read it.
    """
    update = _FakeUpdate(
        effective_user=_FakeUser(id=tg_user_id, username="someone"),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})
    return update, ctx


async def test_invite_onboarded_caller_mints_code_and_replies() -> None:
    """Happy path — any onboarded source mints + receives the code."""
    client = AsyncMock()
    client.create_invite.return_value = {
        "code": "abc123XYZ",
        "expires_at": "2026-06-22T12:00:00+00:00",
    }

    update, ctx = _build_update_and_context(client=client)
    await invite_command(update, ctx)  # type: ignore[arg-type]

    client.create_invite.assert_awaited_once_with(tg_user_id=CALLER_ID, expires_in_days=14)
    assert update.message is not None
    assert len(update.message.replies) == 1
    body, kwargs = update.message.replies[0]
    # MarkdownV2 wrapped — the code is in a backtick block.
    assert "`abc123XYZ`" in body
    assert kwargs.get("parse_mode") == "MarkdownV2"
    # Bot-voice copy: no "operator" framing, no "admin" anywhere.
    lower = body.lower()
    assert "operator" not in lower
    assert "admin" not in lower


async def test_invite_renders_expiry_as_human_utc_not_raw_iso() -> None:
    """The expiry must be ``"June 22 @ 12:00 UTC"`` shape, NOT raw ISO.

    AGENTS.md bot-voice rule: "No ISO timestamps in user-visible
    text. Calendar dates only." The previous template spliced the
    raw ``expires_at`` field straight in, so users saw
    ``"Expires 2026-06-22T12:00:00+00:00."``. This pins the human
    shape so a future template tweak can't silently regress.

    Both the ``T`` separator and the ``+00:00`` offset would only
    appear in the raw ISO — their absence is the contract.
    """
    client = AsyncMock()
    client.create_invite.return_value = {
        "code": "expiryShape",
        "expires_at": "2026-06-22T12:00:00+00:00",
    }

    update, ctx = _build_update_and_context(client=client)
    await invite_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    # Human shape — month spelt out, day without leading zero, UTC
    # suffix. Periods are MarkdownV2-escaped (``\.``); we strip the
    # escape before substring-matching so the assertion focuses on
    # the rendered shape, not the markdown plumbing.
    plain = body.replace("\\", "")
    assert "June 22 @ 12:00 UTC" in plain
    # Raw ISO leftovers — none of these may appear in user-facing text.
    assert "2026-06-22T12:00:00" not in plain
    assert "+00:00" not in plain


async def test_invite_no_longer_reads_admin_allowlist() -> None:
    """The handler must not require ``BOT_DATA_ADMIN_KEY`` in bot_data.

    Pins the rollout invariant: the admin gate has fully moved to the
    server. If the bot ever silently re-checks an allowlist client-side
    again, this test will fail because bot_data is intentionally only
    carrying the API client.
    """
    client = AsyncMock()
    client.create_invite.return_value = {
        "code": "openCode",
        "expires_at": "2026-06-22T12:00:00+00:00",
    }

    update, ctx = _build_update_and_context(client=client)
    assert "admin_chat_ids" not in ctx.bot_data
    await invite_command(update, ctx)  # type: ignore[arg-type]

    # The API still got called — no short-circuit on the way in.
    client.create_invite.assert_awaited_once()


async def test_invite_api_403_replies_with_join_first_nudge() -> None:
    """API ``403 not_a_source`` → friendly "send /start first" copy.

    Pins the bot's translation of the new server-side gate. The user
    typed ``/invite`` before completing ``/start``; we don't want a
    cryptic "try again in a moment" — we want them to know exactly
    what to do.
    """
    client = AsyncMock()
    client.create_invite.side_effect = MerlinHTTPError(403, "Forbidden", detail="not_a_source")

    update, ctx = _build_update_and_context(client=client)
    await invite_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    assert len(update.message.replies) == 1
    body, _ = update.message.replies[0]
    assert "/start" in body
    # NOT the generic retry copy — that would be a UX bug because
    # retrying without /start'ing will 403 again forever.
    assert "try again" not in body.lower()


async def test_invite_api_5xx_replies_temporary_error() -> None:
    """Transient API outage → recoverable "try again" copy.

    Any non-403 API error is a real outage from the user's POV.
    Retrying actually has a chance of working, unlike the 403 path,
    so the copy is different on purpose.
    """
    client = AsyncMock()
    client.create_invite.side_effect = MerlinHTTPError(503, "unavailable")

    update, ctx = _build_update_and_context(client=client)
    await invite_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try again" in body.lower()
