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
    _headline_stats_line,
    _render_me,
    _render_me_rich,
    _utc_datetime,
    build_handler,
    me_command,
)
from merlin_client.errors import MerlinHTTPError
from telegram.error import TelegramError
from telegram.ext import CommandHandler
from telegram.warnings import PTBUserWarning

_ = PTBUserWarning  # re-exported so the type checker keeps the symbol

USER_ID = 42
CHAT_ID = 999


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeMessage:
    chat_id: int = CHAT_ID
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append((text, kw))


@dataclass
class _FakeBot:
    """Captures raw ``_post`` (rich send) + ``send_message`` (fallback)."""

    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    post_side_effect: Exception | None = None

    async def _post(self, endpoint: str, data: dict[str, Any] | None = None, **_kw: Any) -> Any:
        self.posts.append((endpoint, data or {}))
        if self.post_side_effect is not None:
            raise self.post_side_effect

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent.append((chat_id, text, kw))


@dataclass
class _FakeUpdate:
    effective_user: _FakeUser | None
    message: _FakeMessage | None


@dataclass
class _FakeContext:
    bot_data: dict[str, Any]
    bot: _FakeBot = field(default_factory=_FakeBot)


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
    assert "ETH long — 2 weeks, entry $2,438.12, resolves June 12 @ 16:35 UTC" in text, text
    # BTC row has no entry-price chunk — server's None means we omit
    # the "entry $X" segment entirely (never invent a placeholder).
    assert "BTC short — 1 week, resolves June 5 @ 12:00 UTC" in text
    assert "BTC short — 1 week, entry" not in text
    # Call to action at the bottom.
    assert "/alpha" in text
    assert "/board" in text
    # Look-back CTA paragraph routes the user to the per-ticker
    # breakdown + resolved-call history. Pinning the literal so a
    # future rewording stays loud rather than silently dropping
    # the discoverability hook.
    assert "Tap /tickers for your per-ticker breakdown, /history for past calls." in text, text


def test_format_entry_price_omits_chunk_on_none() -> None:
    """``None`` (legacy / CoinGecko-down row) → empty chunk, NOT
    "entry $0.00". The renderer must skip the segment entirely."""
    from merlin_bot.handlers.me import _format_entry_price

    assert _format_entry_price(None) == ""


def test_format_entry_price_falls_back_on_non_numeric() -> None:
    """Defensive: a wire-shape drift where ``entry_price_usd`` lands
    as a non-numeric string (e.g. ``"unavailable"``) hits the
    ``except (TypeError, ValueError)`` branch and returns empty,
    same as ``None``. The /me line just omits the entry chunk."""
    from merlin_bot.handlers.me import _format_entry_price

    assert _format_entry_price("not-a-number") == ""
    assert _format_entry_price(object()) == ""


def test_horizon_human_handles_missing_and_garbage_values() -> None:
    """Defensive horizon-rendering paths. The bullet line is the
    primary /me content; a malformed horizon mustn't crash it."""
    from merlin_bot.handlers.me import _horizon_human

    # None → "soon" (missing key on a wire row).
    assert _horizon_human(None) == "soon"
    # Empty string → "soon" (legacy DB rows from before the column).
    assert _horizon_human("") == "soon"
    # Negative / zero (impossible via the CHECK constraint but
    # defensive) → "soon".
    assert _horizon_human(0) == "soon"
    assert _horizon_human(-1) == "soon"
    # Non-int garbage → "soon" via except (TypeError, ValueError).
    assert _horizon_human("garbage") == "soon"
    assert _horizon_human(object()) == "soon"


def test_direction_human_falls_back_on_unknown_value() -> None:
    """``long`` / ``short`` pass through; anything else (e.g. a
    future enum extension or a malformed wire row) falls back to
    ``"?"`` so the bullet line still reads as a sentence."""
    from merlin_bot.handlers.me import _direction_human

    assert _direction_human("long") == "long"
    assert _direction_human("short") == "short"
    # Truthy unknown value → echoed back (forward-compat for a
    # future "neutral" direction without a code change).
    assert _direction_human("neutral") == "neutral"
    # Empty / falsy → explicit "?" placeholder, never an empty span.
    assert _direction_human("") == "?"


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
    assert "ETH long — 2 weeks, resolves June 12 @ 16:35 UTC" in text
    assert "entry" not in text.lower()


def test_open_call_bullet_glyph_is_real_unicode_bullet() -> None:
    """The bullet glyph MUST be U+2022 (Unicode BULLET), not the
    ASCII bullet substitute "*" or a dot.

    * U+2022 renders as a visible round bullet in Telegram's font,
      lines up across all clients, and doesn't need parse_mode
      escaping (it's not a Markdown special char).
    * If a future refactor copy-pastes the line from a doc tool that
      auto-replaces "*" / "•" / "·", we'd silently regress to an
      ASCII glyph that reads as inline emphasis or a multiplication
      dot. This pin keeps that loud.
    """
    from merlin_bot.handlers.me import _OPEN_CALL_BULLET

    assert _OPEN_CALL_BULLET == "\u2022"
    assert len(_OPEN_CALL_BULLET) == 1


def test_render_me_open_call_lines_use_bullet_prefix() -> None:
    """Each open call MUST start with a "• " bullet — two-space
    indentation reads as flat against the "Open calls:" header in the
    Telegram font, which is what the user reported. Pin the visible
    glyph (U+2022) so a future "let's drop the bullets" refactor flips
    the suite red.
    """
    payload = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "claim_id": "a",
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon_seconds": 1_209_600,
                "resolves_at": "2030-06-12T16:35:00Z",
            },
            {
                "claim_id": "b",
                "asset_symbol": "BTC",
                "direction": "short",
                "horizon_seconds": 604_800,
                "resolves_at": "2030-06-05T12:00:00Z",
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me(payload)
    # Every open-call line starts with "• " — verified by counting how
    # many lines satisfy the rule against the known count.
    bulleted = [line for line in text.splitlines() if line.startswith("• ")]
    assert len(bulleted) == 2, (
        f"expected one bullet per open call (got {len(bulleted)}); raw text was:\n{text}"
    )
    assert "• ETH long" in text
    assert "• BTC short" in text
    # The OLD two-space-indent form must NOT appear — that's exactly
    # the regression we're fixing.
    assert "\n  ETH long" not in text
    assert "\n  BTC short" not in text


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
        "weighted_pnl_pct": 1.85,
        "rank": 3,
    }
    text = _render_me(payload)
    assert "Open: 0   Resolved: 5" in text
    # PnL primary + hit secondary + calls count, same chunk order as
    # /board. Plural ``calls`` because resolved_total > 1.
    assert "PnL +1.85%  ·  60% hit  ·  5 calls" in text


def test_render_me_no_display_name_uses_you_fallback() -> None:
    """User who hasn't set a display name yet — header reads 'you'
    rather than 'None'."""
    payload: dict[str, Any] = {
        "display_name": None,
        "calls_open": [],
        "calls_resolved_total": 0,
    }
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
# Rich rendering
# ---------------------------------------------------------------------------


def test_render_me_rich_open_calls_table_with_live_resolves() -> None:
    """The rich render keeps the profile shape (heading + stat strip)
    and lays out open calls as a table whose Resolves column carries a
    live ``tg://time`` countdown entity (with an absolute-UTC fallback
    label). The entry-price column shows the captured snapshot or an
    em-dash."""
    payload: dict[str, Any] = {
        "display_name": "Yuri",
        "calls_open": [
            {
                "asset_symbol": "ETH",
                "direction": "long",
                "horizon_seconds": 1_209_600,
                "resolves_at": "2030-06-12T16:35:00Z",
                "entry_price_usd": 2438.12,
            },
            {
                "asset_symbol": "BTC",
                "direction": "short",
                "horizon_seconds": 604_800,
                "resolves_at": "2030-06-05T12:00:00Z",
                "entry_price_usd": None,
            },
        ],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me_rich(payload)
    assert text.startswith("# Yuri — your calls")
    assert "**Open** 2 · **Resolved** 0" in text
    assert "## Open calls" in text
    assert "| Asset | Direction | Horizon | Entry | Resolves |" in text
    # ETH row: captured entry + live countdown entity (fallback label is
    # the absolute UTC string the plain renderer shows).
    assert "| ETH | long | 2 weeks | $2,438.12 | " in text
    assert "![June 12 @ 16:35 UTC](tg://time?unix=" in text
    assert "&format=r)" in text
    # BTC row: no captured entry → em-dash, never a faked $0.00.
    assert "| BTC | short | 1 week | — | " in text


def test_render_me_rich_resolved_state_stat_strip() -> None:
    """Once outcomes land the stat strip shows bold PnL / Hit / calls,
    PnL primary with two decimals (matching /board)."""
    payload = {
        "display_name": "Alice",
        "calls_open": [],
        "calls_resolved_total": 5,
        "accuracy_pct": 60.0,
        "weighted_pnl_pct": 1.85,
    }
    text = _render_me_rich(payload)
    assert "**PnL** +1.85% · **Hit** 60% · **5 calls**" in text
    # Zero-open collapses to the honest one-liner, no empty table.
    assert "No open calls yet" in text
    assert "## Open calls" not in text


def test_render_me_rich_cold_start_uses_tracking_copy() -> None:
    """Null PnL / accuracy → the italic tracking line, never a faked
    +0.00%."""
    payload: dict[str, Any] = {
        "display_name": "Yuri",
        "calls_open": [],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me_rich(payload)
    assert "_Accuracy: tracking your first resolutions_" in text


def test_render_me_rich_escapes_markdown_in_display_name() -> None:
    """A display name with Markdown/HTML metacharacters must be escaped
    in the heading so it renders literally and can't inject formatting
    or an HTML tag (Rich Markdown allows embedded HTML)."""
    payload: dict[str, Any] = {
        "display_name": "*<b>x</b>*",
        "calls_open": [],
        "calls_resolved_total": 0,
        "accuracy_pct": None,
    }
    text = _render_me_rich(payload)
    first_line = text.splitlines()[0]
    assert "<b>" not in first_line
    assert "&lt;b&gt;" in first_line
    assert r"\*" in first_line


# ---------------------------------------------------------------------------
# Accuracy line
# ---------------------------------------------------------------------------


def test_headline_stats_line_cold_start_shows_tracking_copy() -> None:
    """Null PnL OR null accuracy OR zero resolved → "tracking your
    first resolutions". Three independent gates because the bot must
    never invent a misleading +0.00% / 0% hit on cold-start."""
    assert (
        _headline_stats_line(
            weighted_pnl_pct=None,
            accuracy_pct=None,
            resolved_total=0,
        )
        == "Accuracy: tracking your first resolutions"
    )
    # Zero resolved with non-null numbers (impossible via CHECK
    # constraint, but defensive against a future schema relaxation).
    assert (
        _headline_stats_line(
            weighted_pnl_pct=0.0,
            accuracy_pct=0.0,
            resolved_total=0,
        )
        == "Accuracy: tracking your first resolutions"
    )
    # PnL missing but accuracy present (impossible via CHECK; pinning
    # for forward-compat).
    assert (
        _headline_stats_line(
            weighted_pnl_pct=None,
            accuracy_pct=80.0,
            resolved_total=5,
        )
        == "Accuracy: tracking your first resolutions"
    )


def test_headline_stats_line_with_data_renders_pnl_primary() -> None:
    """PnL primary, hit rate secondary, calls count. Same chunk order
    as /board so users see consistent numbers in both views."""
    text = _headline_stats_line(
        weighted_pnl_pct=4.20,
        accuracy_pct=75.0,
        resolved_total=8,
    )
    assert text == "PnL +4.20%  ·  75% hit  ·  8 calls"


def test_headline_stats_line_pluralises_call_count() -> None:
    """``1 call`` (singular) / ``2 calls`` (plural)."""
    assert "1 call" in _headline_stats_line(
        weighted_pnl_pct=-1.3,
        accuracy_pct=0.0,
        resolved_total=1,
    )
    assert " 1 calls" not in _headline_stats_line(
        weighted_pnl_pct=-1.3,
        accuracy_pct=0.0,
        resolved_total=1,
    )
    assert "2 calls" in _headline_stats_line(
        weighted_pnl_pct=2.0,
        accuracy_pct=50.0,
        resolved_total=2,
    )


def test_headline_stats_line_falls_back_on_non_numeric_pnl() -> None:
    """Defensive: ``_headline_stats_line`` falls into its ``except
    (TypeError, ValueError)`` branch when PnL or accuracy can't be
    coerced to ``float``. That shouldn't happen via the contract,
    but a wire-shape drift (string instead of number) must NOT take
    out the whole /me reply. We fall back to the cold-start copy.
    """
    text = _headline_stats_line(
        weighted_pnl_pct="not-a-number",
        accuracy_pct=60.0,
        resolved_total=5,
    )
    assert text == "Accuracy: tracking your first resolutions"

    text = _headline_stats_line(
        weighted_pnl_pct=4.2,
        accuracy_pct="abc",
        resolved_total=5,
    )
    assert text == "Accuracy: tracking your first resolutions"


def test_headline_stats_line_signs_positive_and_negative_pnl() -> None:
    """``+1.30%`` / ``-1.30%`` / ``+0.00%`` — sign is always explicit."""
    assert "PnL +1.30%" in _headline_stats_line(
        weighted_pnl_pct=1.3,
        accuracy_pct=100.0,
        resolved_total=1,
    )
    assert "PnL -1.30%" in _headline_stats_line(
        weighted_pnl_pct=-1.3,
        accuracy_pct=0.0,
        resolved_total=1,
    )
    assert "PnL +0.00%" in _headline_stats_line(
        weighted_pnl_pct=0.0,
        accuracy_pct=50.0,
        resolved_total=2,
    )


# ---------------------------------------------------------------------------
# UTC timestamp
# ---------------------------------------------------------------------------


def test_utc_datetime_normalizes_z_and_offset() -> None:
    """``Z`` and ``+00:00`` produce identical output."""
    assert _utc_datetime("2026-06-12T16:35:00Z") == "June 12 @ 16:35 UTC"
    assert _utc_datetime("2026-06-12T16:35:00+00:00") == "June 12 @ 16:35 UTC"


def test_utc_datetime_converts_non_utc_offset() -> None:
    """A non-UTC offset must convert to UTC before rendering."""
    # 16:35 PDT = 23:35 UTC
    assert _utc_datetime("2026-06-12T16:35:00-07:00") == "June 12 @ 23:35 UTC"


def test_utc_datetime_renders_human_month_name() -> None:
    """Full month name + day-without-leading-zero on early dates.

    Pins the format precisely so a regression toward ``YYYY-MM-DD HH:MM
    UTC`` (the old computerish shape) is loud."""
    assert _utc_datetime("2026-01-05T09:07:00Z") == "January 5 @ 09:07 UTC"
    assert _utc_datetime("2026-12-31T23:59:00Z") == "December 31 @ 23:59 UTC"


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
    assert out == "resolves June 4 @ 20:00 UTC"


def test_resolves_at_chunk_at_exactly_24h_boundary_renders_absolute() -> None:
    """At the 24h boundary inclusive, stay on the absolute format.
    Pinning the off-by-one explicitly so a refactor of the threshold
    can't silently flip the boundary inclusive/exclusive."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 0, tzinfo=UTC)
    out = _resolves_at_chunk("2026-06-03T20:00:00Z", now=now)
    assert out == "resolves June 3 @ 20:00 UTC"


def test_resolves_at_chunk_overdue_renders_absolute() -> None:
    """A negative delta (resolver hasn't picked it up yet) renders the
    absolute UTC — never "in -5 minutes". The user sees the same shape
    they saw on the /alpha receipt, and the rendering is honest about
    why nothing has happened."""
    from merlin_bot.handlers.me import _resolves_at_chunk

    now = datetime(2026, 6, 2, 20, 0, tzinfo=UTC)
    out = _resolves_at_chunk("2026-06-02T19:00:00Z", now=now)
    assert out == "resolves June 2 @ 19:00 UTC"


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
    assert "resolves in 3 hours 14 minutes" in text or "resolves in 3 hours 15 minutes" in text, (
        text
    )


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_me_command_fetches_and_renders() -> None:
    """Happy path: /me hits the API with the caller's tg_user_id,
    then sends the rendered payload as a Rich Message."""
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
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await me_command(update, ctx)  # type: ignore[arg-type]

    client.get_me.assert_awaited_once_with(tg_user_id=USER_ID)
    assert update.message is not None
    assert update.message.replies == []
    assert len(bot.posts) == 1
    endpoint, data = bot.posts[0]
    assert endpoint == "sendRichMessage"
    assert data["chat_id"] == CHAT_ID
    markdown = data["rich_message"]["markdown"]
    assert "# Yuri — your calls" in markdown
    assert "**Open** 0 · **Resolved** 0" in markdown


async def test_me_command_falls_back_to_plain_when_rich_rejected() -> None:
    """Telegram rejecting the rich payload falls back to the plain
    ``send_message`` render so the user still sees their calls."""
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
    bot = _FakeBot(post_side_effect=TelegramError("rich not supported"))
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await me_command(update, ctx)  # type: ignore[arg-type]

    assert len(bot.posts) == 1
    assert len(bot.sent) == 1
    sent_chat, sent_text, _kw = bot.sent[0]
    assert sent_chat == CHAT_ID
    assert sent_text.startswith("Yuri — your calls")


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


async def test_me_command_recovers_from_transport_failure() -> None:
    """Non-``MerlinHTTPError`` exceptions (DNS, ssl, timeouts, ...)
    hit the broader ``except Exception`` branch and produce the
    recoverable temporary-error copy — never a raw stacktrace into
    the user's chat."""
    client = AsyncMock()
    client.get_me.side_effect = RuntimeError("dns resolution failed")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try" in body.lower() or "moment" in body.lower()
    assert "RuntimeError" not in body
    assert "dns" not in body.lower()


async def test_me_command_handles_missing_message_silently() -> None:
    """``Update.message is None`` (edited channel posts, inline mode)
    must short-circuit — replying via ``None.reply_text`` would raise
    and surface as a Sentry error.
    """
    client = AsyncMock()
    update = _FakeUpdate(effective_user=_FakeUser(id=USER_ID), message=None)
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await me_command(update, ctx)  # type: ignore[arg-type]

    client.get_me.assert_not_called()


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
