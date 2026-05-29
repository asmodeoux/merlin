"""Unit tests for the ``/me`` handler.

Three things to pin:

1. Rendering: the open-call list is one bullet per line, UTC
   timestamps, lower-case direction, human horizon. No internal
   nouns leak.
2. Empty-state: zero open calls reads "No open calls yet" — never a
   blank list. Accuracy reads "tracking your first resolutions" —
   never "0.0%".
3. Error paths: 404 from the API (user never finished /start) → reply
   points at /start; other failures → recoverable temporary-error copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.me import (
    _accuracy_line,
    _render_me,
    _utc_datetime,
    build_handler,
    me_command,
)
from merlin_client.errors import MerlinHTTPError
from telegram.ext import CommandHandler
from telegram.warnings import PTBUserWarning

_ = PTBUserWarning  # re-exported so the type checker keeps the symbol

USER_ID = 42


@dataclass
class _FakeUser:
    id: int


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


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------


def test_render_me_with_open_calls_lists_one_per_line() -> None:
    """Each open call lands on its own bullet line; the message
    follows the four-section layout (header / headline / body / cta)."""
    payload: dict[str, Any] = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "00000000-0000-0000-0000-000000000001",
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon": "14d",
                "resolves_at": "2026-06-12T16:35:00Z",
            },
            {
                "claim_id": "00000000-0000-0000-0000-000000000002",
                "asset_symbol": "BTC",
                "direction": "short",
                "horizon": "7d",
                "resolves_at": "2026-06-05T12:00:00Z",
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
        "rank": None,
    }
    text = _render_me(payload)
    assert text.startswith("Yuri — your calls")
    assert "Open: 2   Resolved: 0" in text
    assert "Accuracy: tracking your first resolutions" in text
    # Bullets one per line, with the asset symbol up-cased and the
    # horizon translated to human words.
    assert "ETH long — 2 weeks, resolves 2026-06-12 16:35 UTC" in text
    assert "BTC short — 1 week, resolves 2026-06-05 12:00 UTC" in text
    # Call to action at the bottom.
    assert "/alpha" in text
    assert "/board" in text


def test_render_me_empty_state_collapses_the_list_section() -> None:
    """Zero open calls — the list section collapses to a single line."""
    payload = {
        "display_name": "Yuri",
        "calls_open": [],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
        "rank": None,
    }
    text = _render_me(payload)
    assert "Open: 0   Resolved: 0" in text
    assert "No open calls yet" in text
    # The bulleted "Open calls:" header must NOT show when the list
    # is empty — that would leave a dangling header.
    assert "Open calls:" not in text


def test_render_me_with_resolved_calls_shows_accuracy_percent() -> None:
    """Once outcomes ship, accuracy renders as a percent — but only
    when there's at least one resolved call to base the number on."""
    payload = {
        "display_name": "Alice",
        "calls_open": [],
        "calls_resolved_total": 5,
        "accuracy_pct": 60.0,
        "rank": 3,
    }
    text = _render_me(payload)
    assert "Open: 0   Resolved: 5" in text
    assert "Accuracy: 60% (5 resolved)" in text


def test_render_me_no_display_name_uses_you_fallback() -> None:
    """User who hasn't set a display name yet — header reads 'you'
    rather than 'None'."""
    payload = {"display_name": None, "calls_open": [], "calls_resolved_total": 0}
    text = _render_me(payload)
    assert text.startswith("you — your calls")


def test_render_me_avoids_internal_jargon() -> None:
    """Bot voice — no source/claim/regret/belief score leaks."""
    payload = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "x",
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon": "14d",
                "resolves_at": "2026-06-12T16:35:00Z",
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me(payload).lower()
    forbidden = (
        "source",
        "claim",
        "hypothesis",
        "outcome",
        "regret",
        "belief score",
        "simulated",
        "shrinkage",
        "bayesian",
        "v0",
        "phase 2",
        # No raw horizon codes — those are wire-level, not user-facing.
        "14d",
        "7d",
        "30d",
        "90d",
    )
    for term in forbidden:
        assert term not in text, f"/me text leaks internal term: {term!r}"


# ---------------------------------------------------------------------------
# Accuracy line
# ---------------------------------------------------------------------------


def test_accuracy_line_none_or_zero_resolves_to_tracking_copy() -> None:
    """Null accuracy OR zero resolved → "tracking your first
    resolutions". Never "0.0%" — that's the lie the contract change
    is meant to prevent."""
    assert _accuracy_line(None, 0) == "Accuracy: tracking your first resolutions"
    # Edge case: accuracy somehow non-null but zero resolved (should
    # never happen, but we don't want a "0% (0 resolved)" rendering).
    assert _accuracy_line(0.0, 0) == "Accuracy: tracking your first resolutions"


def test_accuracy_line_with_data_shows_percent_and_count() -> None:
    assert _accuracy_line(75.0, 8) == "Accuracy: 75% (8 resolved)"


# ---------------------------------------------------------------------------
# UTC timestamp
# ---------------------------------------------------------------------------


def test_utc_datetime_normalizes_z_and_offset() -> None:
    """``Z`` and ``+00:00`` produce identical output."""
    assert _utc_datetime("2026-06-12T16:35:00Z") == "2026-06-12 16:35 UTC"
    assert _utc_datetime("2026-06-12T16:35:00+00:00") == "2026-06-12 16:35 UTC"


def test_utc_datetime_converts_non_utc_offset() -> None:
    """A non-UTC offset must convert to UTC before rendering."""
    # 16:35 PDT = 23:35 UTC
    assert _utc_datetime("2026-06-12T16:35:00-07:00") == "2026-06-12 23:35 UTC"


def test_utc_datetime_falls_back_to_soon_on_garbage() -> None:
    """A malformed server response must not crash the bot."""
    assert _utc_datetime("") == "soon"
    assert _utc_datetime("not a date") == "soon"


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_me_command_fetches_and_renders() -> None:
    """Happy path: /me hits the API with the caller's tg_user_id,
    then replies with the rendered payload."""
    client = AsyncMock()
    client.get_me.return_value = {
        "display_name": "Yuri",
        "calls_open": [],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    client.get_me.assert_awaited_once_with(tg_user_id=USER_ID)
    assert update.message is not None
    assert len(update.message.replies) == 1
    body, _kw = update.message.replies[0]
    assert "Yuri" in body
    assert "Open: 0" in body


async def test_me_command_404_points_at_start() -> None:
    """User typed /me but never finished /start. The reply must point
    them at /start — silently failing is the original UX bug."""
    client = AsyncMock()
    client.get_me.side_effect = MerlinHTTPError(404, "source not found")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "/start" in body


async def test_me_command_recoverable_error_reply() -> None:
    """A 5xx from the API → recoverable "try again" copy, NEVER the
    raw error."""
    client = AsyncMock()
    client.get_me.side_effect = MerlinHTTPError(503, "down")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try" in body.lower() or "moment" in body.lower()
    # No raw error / status codes leak.
    assert "503" not in body
    assert "down" not in body.lower()


async def test_me_command_handles_missing_effective_user_silently() -> None:
    """An Update with no ``effective_user`` (e.g. a channel post
    replay) must NOT crash the handler — just no-op."""
    client = AsyncMock()
    update = _FakeUpdate(effective_user=None, message=_FakeMessage())
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    client.get_me.assert_not_called()
    assert update.message is not None
    assert update.message.replies == []


# ---------------------------------------------------------------------------
# Build_handler
# ---------------------------------------------------------------------------


def test_build_handler_returns_command_handler_for_me() -> None:
    handler = build_handler()
    assert isinstance(handler, CommandHandler)
    assert "me" in handler.commands


# ---------------------------------------------------------------------------
# Build_handler is wired into application.py
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")
def test_me_handler_registered_in_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot's ``build_application`` must include the ``/me``
    handler — otherwise the slash menu lies and the command falls
    through to silence (the original bug)."""
    from merlin_bot.application import build_application
    from merlin_bot.config import Settings

    settings = Settings(
        telegram_bot_token="test-token",
        merlin_api_base_url="http://test",
        merlin_api_internal_token="test-token",
    )
    # Avoid network calls during construction.
    monkeypatch.setattr(
        "merlin_bot.api_dependency.attach_client",
        lambda *_a, **_kw: None,
    )
    application = build_application(settings)
    commands_registered: set[str] = set()
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                commands_registered.update(handler.commands)
    assert "me" in commands_registered
