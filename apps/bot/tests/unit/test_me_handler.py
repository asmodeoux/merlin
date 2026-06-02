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
from datetime import UTC, datetime
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
    follows the four-section layout (header / headline / body / cta).

    Entry-price chunk renders inline when the server captured one
    (ETH below); omitted when ``entry_price_usd`` is ``None`` (BTC
    below — simulates a row submitted while CoinGecko was down).
    """
    # Resolves-at dates are pushed to 2030 so the absolute-branch
    # assertions in this test don't flake the day real-world ``now``
    # falls within 24h of a fixture. Short-horizon (< 24h) rendering is
    # exercised separately in ``test_resolves_at_chunk_renders_relative_*``
    # below where ``now`` is injected explicitly.
    payload: dict[str, Any] = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "00000000-0000-0000-0000-000000000001",
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon_seconds": 1_209_600,
                "resolves_at": "2030-06-12T16:35:00Z",
                "entry_price_usd": 2438.12,
            },
            {
                "claim_id": "00000000-0000-0000-0000-000000000002",
                "asset_symbol": "BTC",
                "direction": "short",
                "horizon_seconds": 604_800,
                "resolves_at": "2030-06-05T12:00:00Z",
                "entry_price_usd": None,  # legacy / CoinGecko-down row
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
    assert "ETH long — 2 weeks, entry $2,438.12, resolves 2030-06-12 16:35 UTC" in text, text
    # BTC row has no entry-price chunk — server's None means we omit
    # the "entry $X" segment entirely (never invent a placeholder).
    assert "BTC short — 1 week, resolves 2030-06-05 12:00 UTC" in text
    assert "BTC short — 1 week, entry" not in text
    # Call to action at the bottom.
    assert "/alpha" in text
    assert "/board" in text


def test_render_open_call_entry_price_matches_receipt_format_per_bucket() -> None:
    """The /me handler and the /alpha receipt both render an entry
    price; they MUST produce the same number for the same input so
    the user sees an identical figure on the Saved card and on /me.

    Bucket thresholds (>=$1 -> 2dp, $0.01 - $1 -> 4dp, <$0.01 -> 8dp) are
    duplicated between ``handlers/me._format_entry_price`` and
    ``alpha.step_preview._format_usd``. This test cross-validates
    the two implementations against the same inputs — if one drifts
    (e.g. someone bumps the threshold on the receipt side only) the
    user would see "$2,438.12" on Saved and a differently-formatted
    "$2438.12" on /me. That asymmetry would be a silent UX bug.
    """
    from merlin_bot.handlers.alpha import step_preview as sp
    from merlin_bot.handlers.me import _format_entry_price

    samples = [
        2438.12,  # >= $1 — thousands sep + 2dp
        12345.6789,  # >= $1, larger, ensures 2dp truncation matches
        0.4231,  # mid-range — 4dp
        0.0123,  # right above the 4dp threshold
        0.000123,  # sub-cent — 8dp
    ]
    for price in samples:
        receipt_dollars = sp._format_usd(price)
        me_chunk = _format_entry_price(price)
        # ``_format_entry_price`` wraps the formatted dollar amount
        # in "entry $X, ". Strip the affixes and compare the raw
        # dollar token byte-for-byte.
        assert me_chunk.startswith("entry "), me_chunk
        assert me_chunk.endswith(", "), me_chunk
        me_dollars = me_chunk.removeprefix("entry ").removesuffix(", ")
        assert me_dollars == receipt_dollars, (
            f"price={price!r}: /me renders {me_dollars!r} but the "
            f"receipt renders {receipt_dollars!r}; the two adaptive-"
            "decimal helpers have drifted apart."
        )


def test_render_me_omits_entry_price_when_field_absent() -> None:
    """Older API payloads / legacy rows have no ``entry_price_usd``
    key at all — the renderer must treat that as "no snapshot" and
    omit the inline chunk, same as an explicit ``None``."""
    payload = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "x",
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon_seconds": 1_209_600,
                "resolves_at": "2030-06-12T16:35:00Z",
                # no entry_price_usd key at all
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me(payload)
    assert "ETH long — 2 weeks, resolves 2030-06-12 16:35 UTC" in text
    assert "entry" not in text.lower()


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
                "horizon_seconds": 1_209_600,
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
# Resolves-at chunk — relative vs absolute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        # Hours + minutes.
        (2 * 3600 + 11 * 60, "2 hours 11 minutes"),
        # Singular hour + plural minute.
        (3600 + 5 * 60, "1 hour 5 minutes"),
        # Singular hour + singular minute.
        (3600 + 60, "1 hour 1 minute"),
        # Whole hours suppress the trailing "0 minutes".
        (3 * 3600, "3 hours"),
        (3600, "1 hour"),
        # Minutes-only.
        (45 * 60, "45 minutes"),
        (60, "1 minute"),
        # Sub-minute → honest copy, never "0 minutes".
        (15, "less than a minute"),
        (1, "less than a minute"),
    ],
)
def test_humanise_seconds_left_pluralises_correctly(seconds: int, expected: str) -> None:
    """Plural / singular boundaries, zero suppression, sub-minute copy."""
    from merlin_bot.handlers.me import _humanise_seconds_left

    assert _humanise_seconds_left(seconds) == expected


def test_resolves_at_chunk_renders_relative_when_within_24h() -> None:
    """Deadlines < 24h away render as ``resolves in X hours Y minutes``
    — the friendly countdown the user asked for."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 39, tzinfo=UTC)
    # 2026-06-02 22:50 UTC - 2026-06-02 20:39 UTC = 2h 11m
    out = _resolves_at_chunk("2026-06-02T22:50:00Z", now=now)
    assert out == "resolves in 2 hours 11 minutes"


def test_resolves_at_chunk_renders_minutes_only_for_short_window() -> None:
    """Within an hour, drop the leading "0 hours"."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 39, tzinfo=UTC)
    out = _resolves_at_chunk("2026-06-02T21:00:00Z", now=now)
    assert out == "resolves in 21 minutes"


def test_resolves_at_chunk_renders_absolute_when_more_than_24h() -> None:
    """Deadlines >= 24h away keep the absolute UTC format — "in 3 days
    4 hours" is harder to plan against than the calendar timestamp."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 0, tzinfo=UTC)
    # +48h
    out = _resolves_at_chunk("2026-06-04T20:00:00Z", now=now)
    assert out == "resolves 2026-06-04 20:00 UTC"


def test_resolves_at_chunk_at_exactly_24h_boundary_renders_absolute() -> None:
    """At the 24h boundary inclusive, stay on the absolute format.
    Pinning the off-by-one explicitly so a refactor of the threshold
    can't silently flip the boundary inclusive/exclusive."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 0, tzinfo=UTC)
    out = _resolves_at_chunk("2026-06-03T20:00:00Z", now=now)
    assert out == "resolves 2026-06-03 20:00 UTC"


def test_resolves_at_chunk_overdue_renders_absolute() -> None:
    """A negative delta (resolver hasn't picked it up yet) renders the
    absolute UTC — never "in -5 minutes". The user sees the same shape
    they saw on the /alpha receipt, and the rendering is honest about
    why nothing has happened."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 0, tzinfo=UTC)
    out = _resolves_at_chunk("2026-06-02T19:00:00Z", now=now)
    assert out == "resolves 2026-06-02 19:00 UTC"


def test_resolves_at_chunk_falls_back_on_malformed_iso() -> None:
    """Defensive — a malformed server timestamp must NOT crash the bot."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    assert _resolves_at_chunk("") == "resolves soon"
    assert _resolves_at_chunk("not a date") == "resolves soon"


def test_resolves_at_chunk_integrates_into_open_call_line() -> None:
    """End-to-end through ``_render_me``: a fixture < 24h from a frozen
    ``now`` (via the helper's ``now=`` param exercised above) reaches
    the bullet line. Here we verify the production callsite plumbs the
    chunk into the right spot — the test isn't time-frozen, so we
    assert on a < 24h horizon by computing the resolves_at from
    real ``datetime.now`` plus a small offset."""
    from datetime import timedelta

    from merlin_bot.handlers.me import _render_me

    soon = datetime.now(UTC) + timedelta(hours=3, minutes=15)
    payload = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "x",
                "asset_symbol": "TON",
                "direction": "short",
                "horizon_seconds": 86_400,
                "resolves_at": soon.isoformat().replace("+00:00", "Z"),
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me(payload)
    # Hours + minutes counted at runtime. Tolerate the ±1-minute drift
    # between the helper's ``int()`` floor and the wall-clock tick that
    # may have advanced between ``timedelta()`` and ``_render_me``.
    assert "TON short — 24 hours, resolves in " in text, text
    assert (
        "resolves in 3 hours 14 minutes" in text or "resolves in 3 hours 15 minutes" in text
    ), text


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
