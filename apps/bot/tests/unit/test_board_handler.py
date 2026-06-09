"""Unit tests for the ``/board`` handler.

Pins three layered things:

* Empty-state copy — honest, no version markers.
* Row rendering — medal-emoji top-3, plain rank-numbers below, PnL
  primary, hit-rate secondary, calls count. HTML parse mode; the
  caller's display name is wrapped in ``<b>`` when /me identifies them.
* Handler wiring — ``/board`` is registered as a CommandHandler, the
  command calls ``GET /board`` (and best-effort ``GET /me``), and a
  5xx degrades to a recoverable error message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.board import (
    CB_BOARD_FILTER_ALL,
    CB_BOARD_FILTER_DAY,
    CB_BOARD_FILTER_POSITION,
    CB_BOARD_FILTER_SWING,
    _empty_state_text,
    _filter_keyboard,
    _render_board,
    _render_row,
    board_command,
    build_callback_handler,
    build_handler,
    on_filter_callback,
)
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler
from telegram.warnings import PTBUserWarning

_ = PTBUserWarning  # re-exported so the type checker keeps the symbol


@dataclass
class _FakeMessage:
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append((text, kw))


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeUpdate:
    message: _FakeMessage | None
    effective_user: _FakeUser | None = None


@dataclass
class _FakeContext:
    bot_data: dict[str, Any]


def _row_entry(
    *,
    rank: int = 1,
    name: str = "Alice",
    pnl: float | None = 2.5,
    accuracy: float | None = 80.0,
    resolved: int = 10,
    correct: int = 8,
) -> dict[str, Any]:
    """Build a board-row dict in the shape the API returns."""
    return {
        "rank": rank,
        "display_name": name,
        "weighted_pnl_pct": pnl,
        "accuracy_pct": accuracy,
        "calls_resolved": resolved,
        "calls_correct": correct,
    }


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_state_text_frames_as_product_choice() -> None:
    """The honest empty-state — no "coming soon" / "v0" / version
    markers. Reads as a product decision."""
    text = _empty_state_text()
    assert "Leaderboard unlocks" in text
    assert "/alpha" in text
    assert "/me" in text
    forbidden = ("coming soon", "v0", "phase 2", "tbd", "soon™")
    lowered = text.lower()
    for term in forbidden:
        assert term not in lowered, f"empty-state leaks {term!r}"


def test_render_board_empty_returns_empty_state() -> None:
    """Empty ``entries`` → honest empty-state copy."""
    assert _render_board({"entries": [], "window": "all_time"}) == _empty_state_text()


def test_render_board_missing_entries_key_returns_empty_state() -> None:
    """A defensive ``None``/missing field must also render the empty
    state (not crash)."""
    assert _render_board({"window": "all_time"}) == _empty_state_text()


# ---------------------------------------------------------------------------
# Row rendering — medal emojis, PnL primary, hit-rate secondary
# ---------------------------------------------------------------------------


def test_render_row_uses_gold_medal_for_rank_one() -> None:
    """🥇 prefix for rank 1, with PnL primary + hit secondary + calls."""
    row = _row_entry(rank=1, name="Alice", pnl=4.2, accuracy=80.0, resolved=10)
    text = _render_row(row, self_display_name=None)
    assert text.startswith("🥇 ")
    assert "Alice" in text
    assert "+4.20% PnL" in text
    assert "80% hit" in text
    assert "10 calls" in text


def test_render_row_uses_silver_medal_for_rank_two() -> None:
    """🥈 prefix for rank 2."""
    row = _row_entry(rank=2, name="Bob", pnl=-1.5, accuracy=43.0, resolved=7)
    text = _render_row(row, self_display_name=None)
    assert text.startswith("🥈 ")
    assert "-1.50% PnL" in text


def test_render_row_uses_bronze_medal_for_rank_three() -> None:
    """🥉 prefix for rank 3."""
    row = _row_entry(rank=3, name="Carla")
    text = _render_row(row, self_display_name=None)
    assert text.startswith("🥉 ")


@pytest.mark.parametrize("rank", [4, 5, 7, 10])
def test_render_row_uses_plain_rank_for_below_podium(rank: int) -> None:
    """Below the podium: ``4. Name — …`` (no emoji)."""
    row = _row_entry(rank=rank, name="Dan")
    text = _render_row(row, self_display_name=None)
    assert text.startswith(f"{rank}. ")


def test_render_row_bolds_callers_name_only() -> None:
    """When ``self_display_name`` matches the row, wrap the NAME in
    ``<b>`` — not the whole row, so the layout stays uniform across
    bold + non-bold rows."""
    row = _row_entry(rank=2, name="Yuri", pnl=1.0, accuracy=50.0, resolved=4)
    text = _render_row(row, self_display_name="Yuri")
    assert "<b>Yuri</b>" in text
    # Other chunks must stay outside the <b>...</b> wrapper.
    assert "<b>+1.00% PnL</b>" not in text
    assert "<b>50% hit</b>" not in text


def test_render_row_does_not_bold_when_self_name_differs() -> None:
    """Different display name → no bold, even if names look similar."""
    row = _row_entry(rank=1, name="alice")  # lowercase variant
    text = _render_row(row, self_display_name="Alice")
    assert "<b>" not in text


def test_render_row_does_not_bold_when_self_name_is_none() -> None:
    """When /me failed and we don't know the caller, NO row is bolded —
    graceful degradation."""
    row = _row_entry(rank=1, name="Alice")
    text = _render_row(row, self_display_name=None)
    assert "<b>" not in text


def test_render_row_escapes_html_in_display_name() -> None:
    """A malicious / odd display name with HTML chars must be escaped
    so we don't break the parse_mode rendering or open injection."""
    row = _row_entry(rank=1, name="<script>alert('x')</script>")
    text = _render_row(row, self_display_name=None)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_render_row_pluralises_calls_count() -> None:
    """``1 call`` (singular) / ``2 calls`` (plural) / ``0 calls``."""
    assert "1 call" in _render_row(_row_entry(resolved=1), self_display_name=None)
    assert " 1 calls" not in _render_row(
        _row_entry(resolved=1),
        self_display_name=None,
    )
    assert "2 calls" in _render_row(_row_entry(resolved=2), self_display_name=None)
    assert "0 calls" in _render_row(_row_entry(resolved=0), self_display_name=None)


def test_render_row_signs_positive_pnl_explicitly() -> None:
    """Positive PnL always shows the ``+`` sign — clearer at a glance
    in a list dominated by signed numbers."""
    assert "+2.50% PnL" in _render_row(
        _row_entry(pnl=2.5),
        self_display_name=None,
    )
    assert "+0.00% PnL" in _render_row(
        _row_entry(pnl=0.0),
        self_display_name=None,
    )


def test_render_row_handles_null_pnl_gracefully() -> None:
    """Defensive: a NULL PnL on a wire row (shouldn't happen — API
    filters cold-start out — but pinning for forward-compat) renders
    an em-dash, not a crash."""
    text = _render_row(_row_entry(pnl=None), self_display_name=None)
    assert "— PnL" in text


def test_render_row_handles_null_accuracy_gracefully() -> None:
    """Same defensive rule for accuracy."""
    text = _render_row(_row_entry(accuracy=None), self_display_name=None)
    assert "— hit" in text


def test_render_row_handles_non_numeric_pnl_gracefully() -> None:
    """Defensive: ``_format_pnl_chunk`` falls into its ``except
    (TypeError, ValueError)`` branch when ``pnl`` is a non-numeric
    string (e.g. a future schema-bug that lets a label through). The
    row renders an em-dash rather than crashing the whole /board
    reply on a single malformed wire field.
    """
    # ``_row_entry`` types ``pnl`` as ``float | None``; pass a wire-
    # shape dict directly to bypass the helper's narrow typing and
    # simulate a malformed server payload.
    row = {
        "rank": 1,
        "display_name": "Alice",
        "weighted_pnl_pct": "not-a-number",
        "accuracy_pct": 80.0,
        "calls_resolved": 5,
        "calls_correct": 4,
    }
    text = _render_row(row, self_display_name=None)
    assert "— PnL" in text  # em-dash fallback fired
    assert "80% hit" in text  # accuracy still rendered normally
    assert "5 calls" in text


def test_render_row_handles_non_numeric_accuracy_gracefully() -> None:
    """Mirror of the PnL case for the accuracy chunk."""
    row = {
        "rank": 1,
        "display_name": "Alice",
        "weighted_pnl_pct": 2.5,
        "accuracy_pct": "not-a-number",
        "calls_resolved": 5,
        "calls_correct": 4,
    }
    text = _render_row(row, self_display_name=None)
    assert "— hit" in text
    assert "+2.50% PnL" in text


# ---------------------------------------------------------------------------
# Full board rendering
# ---------------------------------------------------------------------------


def test_render_board_with_rows_renders_medal_lines_and_footer() -> None:
    """End-to-end render: header, three medal rows, footer, CTA."""
    payload = {
        "window": "all_time",
        "entries": [
            _row_entry(rank=1, name="Alice", pnl=4.2, accuracy=80.0, resolved=10),
            _row_entry(rank=2, name="Bob", pnl=-1.5, accuracy=57.0, resolved=7),
            _row_entry(rank=3, name="Carla", pnl=2.0, accuracy=66.0, resolved=3),
        ],
    }
    text = _render_board(payload)
    assert "<b>Leaderboard</b>" in text
    assert "🥇 Alice — +4.20% PnL · 80% hit · 10 calls" in text
    assert "🥈 Bob — -1.50% PnL · 57% hit · 7 calls" in text
    assert "🥉 Carla — +2.00% PnL · 66% hit · 3 calls" in text
    assert "Ranked by recent PnL, weighted by track record." in text
    assert "/alpha" in text
    assert "/me" in text


# ---------------------------------------------------------------------------
# Footer — platform activity heartbeat ("X open calls by Y users…")
# ---------------------------------------------------------------------------


def test_render_board_footer_omits_open_activity_when_zero() -> None:
    """``open_calls_total == 0`` → footer is the ranking line ALONE.

    "0 open calls by 0 users are in progress" reads as a downtime
    signal even though the leaderboard above may be populated; on
    a green-field deploy it would leak that the platform is empty.
    Cold-start should look quiet, not broken — so we omit the
    activity sentence entirely below the zero threshold.
    """
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
        "open_calls_total": 0,
        "open_callers_total": 0,
    }
    text = _render_board(payload)
    assert "<i>Ranked by recent PnL, weighted by track record.</i>" in text
    assert "in progress" not in text
    assert "open call" not in text


def test_render_board_footer_omits_open_activity_when_field_missing() -> None:
    """No ``open_calls_total`` on the payload (stale schema) → footer is
    the ranking line ALONE. Same omission branch as the zero case;
    pinned separately so a future "treat missing as -1" rewrite stays
    loud."""
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
    }
    text = _render_board(payload)
    assert "<i>Ranked by recent PnL, weighted by track record.</i>" in text
    assert "in progress" not in text


def test_render_board_footer_renders_open_activity_plural() -> None:
    """Pluralised activity sentence: ``22 open calls by 4 users are in progress.``"""
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
        "open_calls_total": 22,
        "open_callers_total": 4,
    }
    text = _render_board(payload)
    assert (
        "<i>Ranked by recent PnL, weighted by track record. "
        "22 open calls by 4 users are in progress.</i>"
    ) in text


def test_render_board_footer_activity_singular_one_call_one_user() -> None:
    """1 call, 1 user → singular nouns + ``is`` (not ``are``).

    Anything else reads wrong: "1 open calls by 1 users is in
    progress" or "1 open call by 1 user are in progress" both
    grate. Pinning all three pluralisation axes (call/calls,
    user/users, is/are) on the singular boundary.
    """
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
        "open_calls_total": 1,
        "open_callers_total": 1,
    }
    text = _render_board(payload)
    assert "1 open call by 1 user is in progress" in text
    assert "1 calls" not in text
    assert "1 users" not in text
    assert "are in progress" not in text


def test_render_board_footer_activity_mixed_singular_user_plural_calls() -> None:
    """``5 open calls by 1 user`` — calls plural, user singular.

    Common shape early on (one prolific caller). The verb still
    keys off ``open_calls_total`` so it stays ``are``, not ``is``.
    """
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
        "open_calls_total": 5,
        "open_callers_total": 1,
    }
    text = _render_board(payload)
    assert "5 open calls by 1 user are in progress" in text


def test_render_board_footer_activity_handles_non_numeric_gracefully() -> None:
    """Defensive: ``open_calls_total = "not-a-number"`` hits the
    ``_safe_int`` fallback and the activity sentence is omitted
    rather than crashing the whole /board reply on a malformed
    wire field."""
    payload = {
        "window": "all_time",
        "entries": [_row_entry(rank=1, name="Alice")],
        "open_calls_total": "not-a-number",
        "open_callers_total": "also-not",
    }
    text = _render_board(payload)
    assert "<i>Ranked by recent PnL, weighted by track record.</i>" in text
    assert "in progress" not in text


def test_render_board_bolds_only_self_row() -> None:
    """``self_display_name=Yuri`` bolds the Yuri row only, leaves
    other rows plain."""
    payload = {
        "window": "all_time",
        "entries": [
            _row_entry(rank=1, name="Alice"),
            _row_entry(rank=2, name="Yuri"),
            _row_entry(rank=3, name="Bob"),
        ],
    }
    text = _render_board(payload, self_display_name="Yuri")
    assert "<b>Yuri</b>" in text
    assert "<b>Alice</b>" not in text
    assert "<b>Bob</b>" not in text


def test_render_board_trims_to_leaderboard_limit() -> None:
    """A flood of entries gets trimmed to ``LEADERBOARD_LIMIT`` to
    keep the message short — Telegram has a 4096-char cap, and a
    50-row table is unhelpful UX anyway.

    Boundary: ``User{LIMIT-1}`` (the user at rank ``LIMIT``, since
    we 0-index the fixture) MUST appear; ``User{LIMIT}`` (rank
    ``LIMIT+1``) MUST NOT, unless they're the caller (covered by
    a separate test).
    """
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    entries = [
        _row_entry(rank=i + 1, name=f"User{i}", pnl=1.0 + i, accuracy=50.0)
        for i in range(LEADERBOARD_LIMIT + 10)
    ]
    text = _render_board({"entries": entries, "window": "all_time"})
    assert f"User{LEADERBOARD_LIMIT - 1}" in text
    assert f"User{LEADERBOARD_LIMIT}" not in text


def test_leaderboard_limit_is_thirty() -> None:
    """The locked cap. Pinned because the user spec explicitly calls
    it out and a future "let's drop it to 10" tweak would silently
    revert the UX everyone signed off on."""
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    assert LEADERBOARD_LIMIT == 30


def test_gap_line_glyph_is_horizontal_ellipsis() -> None:
    """The gap-separator glyph is U+2026 HORIZONTAL ELLIPSIS — a
    single code point, not three ASCII dots. Pinned because:

    * Telegram renders ``…`` more compactly than ``...`` and on its
      own line it reads as a deliberate break rather than three
      trailing dots that look like a typo.
    * Re-typing the glyph from memory often produces ``...`` (three
      dots) instead, which would silently change the rendered look
      on every client.
    """
    from merlin_bot.handlers.board import _GAP_LINE

    assert _GAP_LINE == "\u2026"
    assert len(_GAP_LINE) == 1  # single code point, not "..."


def test_render_board_caller_at_exact_limit_rank_is_inside() -> None:
    """Boundary: a caller at rank exactly == ``LEADERBOARD_LIMIT`` is
    INSIDE (no appendage). A caller at rank ``LEADERBOARD_LIMIT + 1``
    is OUTSIDE (appendage). The off-by-one matters because flipping
    it would either silently hide the rank-30 user from the visible
    list or duplicate the rank-30 user as both inline + appended.
    """
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    # Build a leaderboard where the caller sits exactly at the cap.
    entries = [
        _row_entry(
            rank=i + 1,
            name="Yuri" if i + 1 == LEADERBOARD_LIMIT else f"User{i}",
            pnl=1.0,
        )
        for i in range(LEADERBOARD_LIMIT)
    ]
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=10,
    )
    # Inline only — one bold, no gap.
    assert text.count("<b>Yuri</b>") == 1
    assert "\n…\n" not in text


def test_render_board_caller_one_past_limit_gets_appended() -> None:
    """Mirror of the above: caller at ``LEADERBOARD_LIMIT + 1`` MUST
    be appended (the smallest "outside" case). If this flips we've
    dropped the closest-to-cap users from the board entirely."""
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    entries = [_row_entry(rank=i + 1, name=f"User{i}", pnl=1.0) for i in range(LEADERBOARD_LIMIT)]
    just_past = LEADERBOARD_LIMIT + 1
    entries.append(
        _row_entry(rank=just_past, name="Yuri", pnl=-0.1, accuracy=50.0, resolved=3),
    )
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=3,
    )
    assert "\n…\n" in text
    assert f"{just_past}. <b>Yuri</b>" in text


def test_render_board_appended_row_escapes_html_in_display_name() -> None:
    """A caller whose display_name contains HTML special chars must
    still be HTML-escaped when rendered in the APPENDED row (not
    just inside the visible top-N). Same defence applies on both
    code paths; pinning so a future "skip escape on appendage"
    refactor stays loud.
    """
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    payload_name = "<script>x()</script>"
    entries = [_row_entry(rank=i + 1, name=f"User{i}", pnl=1.0) for i in range(LEADERBOARD_LIMIT)]
    entries.append(
        _row_entry(rank=42, name=payload_name, pnl=0.5, accuracy=40.0, resolved=4),
    )
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name=payload_name,
        self_resolved_total=4,
    )
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    # And it's still wrapped in <b>...</b> for self-highlight.
    assert "<b>&lt;script&gt;x()&lt;/script&gt;</b>" in text


def test_render_board_footer_always_renders_last() -> None:
    """The footer (ranking copy + CTA) is always the LAST two
    non-empty lines, regardless of which appendage branch fired.
    Pins the layout invariant: appendage goes ABOVE the footer, not
    below — otherwise a rank-42 caller would have to scroll past the
    footer to see themselves.
    """
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    def _last_two_nonempty(text: str) -> list[str]:
        nonempty = [line for line in text.splitlines() if line.strip()]
        return nonempty[-2:]

    expected_tail = [
        "<i>Ranked by recent PnL, weighted by track record.</i>",
        "/alpha to add a call, /me for your own.",
    ]

    # Branch A: caller in top-N (no appendage).
    entries_in = [
        _row_entry(
            rank=i + 1,
            name="Yuri" if i == 0 else f"User{i}",
            pnl=1.0,
        )
        for i in range(LEADERBOARD_LIMIT)
    ]
    text_in = _render_board(
        {"entries": entries_in, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=5,
    )
    assert _last_two_nonempty(text_in) == expected_tail

    # Branch B: caller outside top-N (gap + row).
    entries_out = list(entries_in[:-1])  # drop Yuri from top-N
    entries_out.append(
        _row_entry(rank=42, name="Yuri", pnl=-0.1, accuracy=40.0, resolved=5),
    )
    text_out = _render_board(
        {"entries": entries_out, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=5,
    )
    assert _last_two_nonempty(text_out) == expected_tail

    # Branch C: cold-start caller (inspiring line, no row).
    text_cs = _render_board(
        {"entries": [_row_entry(rank=1, name="Alice")], "window": "all_time"},
        self_display_name="Newcomer",
        self_resolved_total=0,
    )
    assert _last_two_nonempty(text_cs) == expected_tail


def test_render_board_footer_with_open_activity_is_last() -> None:
    """The activity sentence is part of the SAME italic line as the
    ranking explainer — together they're still the second-to-last
    line, with the ``/alpha`` CTA below.

    Pins the layout when ``open_calls_total > 0``: a future refactor
    that splits the activity sentence onto its own line would push
    the CTA further down (a regression — the CTA must stay one
    blank-separated line below the italic block).
    """

    def _last_two_nonempty(text: str) -> list[str]:
        nonempty = [line for line in text.splitlines() if line.strip()]
        return nonempty[-2:]

    text = _render_board(
        {
            "entries": [_row_entry(rank=1, name="Alice")],
            "window": "all_time",
            "open_calls_total": 22,
            "open_callers_total": 4,
        },
    )
    assert _last_two_nonempty(text) == [
        (
            "<i>Ranked by recent PnL, weighted by track record. "
            "22 open calls by 4 users are in progress.</i>"
        ),
        "/alpha to add a call, /me for your own.",
    ]


# ---------------------------------------------------------------------------
# _safe_int — defensive int coercion
# ---------------------------------------------------------------------------


def test_safe_int_returns_zero_for_none() -> None:
    """``None`` is the most realistic missing-field shape (``dict.get``
    on an absent key). MUST return 0, never raise.
    """
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int(None) == 0


def test_safe_int_passes_through_int_unchanged() -> None:
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int(42) == 42
    assert _safe_int(0) == 0
    assert _safe_int(-1) == -1


def test_safe_int_truncates_float() -> None:
    """A float-typed rank shouldn't happen (Pydantic narrows to int)
    but if it does, truncate rather than crash. We document the
    behaviour so a future ``round()`` rewrite is a deliberate choice."""
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int(3.0) == 3
    assert _safe_int(3.9) == 3  # int() truncates


def test_safe_int_parses_numeric_string() -> None:
    """JSON wires ints as ints, but a buggy server could return a
    stringified rank. Coerce rather than crash."""
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int("42") == 42


def test_safe_int_returns_zero_for_non_numeric_string() -> None:
    """``int("abc")`` raises; we MUST swallow it and return 0."""
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int("not-a-number") == 0
    assert _safe_int("") == 0
    assert _safe_int("3.14") == 0  # int() rejects "3.14" via ValueError


def test_safe_int_returns_zero_for_non_supported_type() -> None:
    """Anything not int/float/str hits the isinstance fallback and
    returns 0. Defends against a future schema change that lets a
    dict / list through dict.get()."""
    from merlin_bot.handlers.board import _safe_int

    assert _safe_int({"rank": 4}) == 0
    assert _safe_int([4]) == 0
    assert _safe_int(object()) == 0


# ---------------------------------------------------------------------------
# Handler-level defensive coercion of /me payload
# ---------------------------------------------------------------------------


async def test_board_command_coerces_string_calls_resolved_total() -> None:
    """If /me returned ``calls_resolved_total`` as a stringified int
    (defensive: would be a server bug, but the bot must not crash),
    the handler coerces it to an int and uses it normally. With
    ``"5"`` (truthy + parseable) the caller is treated as having
    resolutions — falls through to anomalous-missing branch (no
    appendage, no inspiring line) since they're not in entries.
    """
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
    }
    client.get_me.return_value = {
        "display_name": "Yuri",
        "calls_resolved_total": "5",  # stringified
    }
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    # Coerced cleanly (no crash). Yuri isn't in entries → anomalous-
    # missing path → no inspiring line, no row.
    assert "Your first resolution" not in body
    assert "Yuri" not in body


async def test_board_command_falls_back_to_zero_on_garbage_calls_resolved_total() -> None:
    """Non-numeric string in ``calls_resolved_total`` hits the
    ``except (TypeError, ValueError)`` branch in the handler and
    falls back to 0 — so the caller is treated as cold-start.
    Defends against a stale schema / proxy that mangles the field."""
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
    }
    client.get_me.return_value = {
        "display_name": "Newcomer",
        "calls_resolved_total": "not-a-number",
    }
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    # Cold-start fallback fires.
    assert "Your first resolution lands you on the board." in body


# ---------------------------------------------------------------------------
# Self-positioning appendage (outside top-N + cold-start)
# ---------------------------------------------------------------------------


def test_render_board_appends_caller_row_below_top_n_with_gap() -> None:
    """When the caller's rank is outside the visible top-N, append:

        …
        {rank}. {caller name} — …

    So a rank-42 caller still sees their position without scrolling
    through 30 unrelated rows. The gap glyph is "…" (U+2026) on its
    own line — visible at a glance, parse-mode safe.
    """
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    entries = [
        _row_entry(rank=i + 1, name=f"User{i}", pnl=1.0 + i) for i in range(LEADERBOARD_LIMIT)
    ]
    # Yuri ranks one past the visible window.
    yuri_rank = LEADERBOARD_LIMIT + 12
    entries.append(
        _row_entry(rank=yuri_rank, name="Yuri", pnl=-0.5, accuracy=40.0, resolved=5),
    )

    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=5,
    )

    # The visible top-N is intact (last in-window row is rendered).
    assert f"User{LEADERBOARD_LIMIT - 1}" in text
    # The first out-of-window row is NOT rendered (not the caller).
    # We use the rank prefix to disambiguate against the caller's own
    # appended row, which carries a higher rank.
    assert f"{LEADERBOARD_LIMIT + 1}." not in text
    # The gap line shows up between the visible top-N and the caller.
    assert "\n…\n" in text, (
        f"expected the U+2026 gap glyph between the visible top-N and "
        f"the caller's appended row; got:\n{text}"
    )
    # The caller's row IS appended, with their real rank prefix,
    # bolded so they recognise themselves.
    assert f"{yuri_rank}. <b>Yuri</b>" in text
    # And the gap appears BEFORE the caller's appended row.
    gap_pos = text.find("\n…\n")
    yuri_pos = text.find(f"{yuri_rank}. <b>Yuri</b>")
    assert 0 < gap_pos < yuri_pos, f"gap (idx={gap_pos}) must precede caller's row (idx={yuri_pos})"


def test_render_board_does_not_append_when_caller_inside_top_n() -> None:
    """Caller inside the visible top-N → no gap, no appended row.
    Their row is already bolded inline; a second copy below would be
    redundant noise."""
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    entries = [
        _row_entry(rank=i + 1, name="Yuri" if i == 5 else f"User{i}", pnl=1.0 + i)
        for i in range(LEADERBOARD_LIMIT)
    ]
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name="Yuri",
        self_resolved_total=10,
    )
    # Only one bold — the inline one.
    assert text.count("<b>Yuri</b>") == 1
    # No gap line.
    assert "\n…\n" not in text


def test_render_board_shows_inspiring_message_for_cold_start_caller() -> None:
    """Caller has zero resolved calls → no appended row, just an
    inspiring one-liner. The inspiring line is the entire appendage:
    no gap glyph, no "your rank: —" placeholder.
    """
    entries = [_row_entry(rank=1, name="Alice")]
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name="Bob",  # caller, but NOT in the entries
        self_resolved_total=0,  # ← cold-start
    )
    assert "Your first resolution lands you on the board." in text
    # Not a fake row.
    assert "Bob — " not in text
    assert "<b>Bob</b>" not in text
    # No gap glyph for cold-start (the inspiring line stands alone).
    assert "\n…\n" not in text


def test_render_board_skips_appendage_when_me_unknown() -> None:
    """``self_display_name=None`` (i.e. /me failed) → no appendage
    of any kind. We don't know who the caller is, can't bold, can't
    append, can't show a cold-start message that might be wrong."""
    entries = [_row_entry(rank=1, name="Alice")]
    text = _render_board(
        {"entries": entries, "window": "all_time"},
        self_display_name=None,
        self_resolved_total=0,
    )
    assert "Your first resolution" not in text
    assert "\n…\n" not in text


def test_render_board_silences_anomalous_caller_missing_with_resolutions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller with ``self_resolved_total > 0`` who isn't in the
    leaderboard at all (mid-recompute snapshot lag) does NOT trigger
    the cold-start message (would mislead) and does NOT trigger an
    appended row (no row exists). The handler logs a warning so we
    can investigate without scaring the user."""
    import logging

    entries = [_row_entry(rank=1, name="Alice")]
    with caplog.at_level(logging.WARNING, logger="merlin.bot.handlers.board"):
        text = _render_board(
            {"entries": entries, "window": "all_time"},
            self_display_name="Yuri",  # NOT in entries
            self_resolved_total=3,  # but supposedly has resolutions
        )
    assert "Your first resolution" not in text
    assert "\n…\n" not in text
    assert "Yuri" not in text  # absolutely no caller mention
    # And we log the anomaly — pinned so we don't silently lose this
    # diagnostic if someone refactors the warning away.
    assert any(
        "Yuri" in rec.message and "missing from /board" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]


def test_render_board_avoids_internal_jargon() -> None:
    """Bot voice — no Bayesian / regret / belief score leaks even on
    the populated path."""
    payload = {
        "window": "all_time",
        "entries": [_row_entry()],
    }
    text = _render_board(payload).lower()
    forbidden = (
        "bayesian",
        "shrinkage",
        "regret",
        "belief score",
        "simulated",
        "v0",
        "phase 2",
        "sort_score",
        "shrunk",
    )
    for term in forbidden:
        assert term not in text, f"/board text leaks {term!r}"


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_board_command_hits_board_and_me_apis() -> None:
    """/board calls get_board AND get_me (best-effort self-highlight).

    The /me payload now carries ``calls_resolved_total`` too — used
    by ``_self_appendage_lines`` to distinguish cold-start callers
    (zero resolved) from outside-top-N callers (resolved but ranked
    deep). We don't assert appendage shape here; that's covered in
    the rendering tests above.
    """
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry()],
        "window": "all_time",
    }
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 10,
    }
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    client.get_board.assert_awaited_once_with(window="all_time", horizon="all")
    client.get_me.assert_awaited_once_with(tg_user_id=12345)
    assert update.message is not None
    body, kwargs = update.message.replies[0]
    # Alice (the caller) gets bolded.
    assert "<b>Alice</b>" in body
    # Parse mode is HTML.
    assert kwargs.get("parse_mode") is not None
    # The horizon filter keyboard is always attached on first render.
    assert kwargs.get("reply_markup") is not None


async def test_board_command_appends_caller_row_when_outside_top_n() -> None:
    """End-to-end: the caller's row gets appended below "…" when
    they rank past the visible top-N. Cross-checks that the handler
    threads ``calls_resolved_total`` from /me into the renderer."""
    from merlin_bot.handlers.board import LEADERBOARD_LIMIT

    client = AsyncMock()
    entries = [_row_entry(rank=i + 1, name=f"User{i}", pnl=1.0) for i in range(LEADERBOARD_LIMIT)]
    entries.append(
        _row_entry(rank=42, name="Yuri", pnl=-0.7, accuracy=40.0, resolved=5),
    )
    client.get_board.return_value = {"entries": entries, "window": "all_time"}
    client.get_me.return_value = {
        "display_name": "Yuri",
        "calls_resolved_total": 5,
    }
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "42. <b>Yuri</b>" in body
    assert "\n…\n" in body


async def test_board_command_shows_inspiring_line_for_cold_start_caller() -> None:
    """End-to-end: a caller with ``calls_resolved_total=0`` gets the
    inspiring line, not a row, not a gap glyph."""
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
    }
    client.get_me.return_value = {
        "display_name": "Newcomer",
        "calls_resolved_total": 0,
    }
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "Your first resolution lands you on the board." in body
    assert "Newcomer" not in body  # no fake row


async def test_board_command_handles_me_missing_calls_resolved_total() -> None:
    """Defensive — if /me returns a payload without ``calls_resolved_total``
    (legacy/stale schema), the handler must default to 0, not crash."""
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
    }
    # No calls_resolved_total key at all.
    client.get_me.return_value = {"display_name": "Newcomer"}
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    # Defaulting to 0 means the caller is treated as cold-start.
    assert "Your first resolution lands you on the board." in body


async def test_board_command_degrades_when_me_fails() -> None:
    """If /me errors, the board still renders — just without
    self-bolding. /board is the must-have."""
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(name="Alice")],
        "window": "all_time",
    }
    client.get_me.side_effect = MerlinHTTPError(404, "not onboarded")
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    # The board still rendered.
    assert "Alice" in body
    # But nobody got bolded.
    assert "<b>Alice</b>" not in body


async def test_board_command_empty_state_when_board_empty() -> None:
    """No entries → honest empty-state, even if /me succeeded."""
    client = AsyncMock()
    client.get_board.return_value = {"entries": [], "window": "all_time"}
    client.get_me.return_value = {"display_name": "Alice"}
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "Leaderboard unlocks" in body


async def test_board_command_short_circuits_when_message_missing() -> None:
    """Defensive: PTB hands us an ``Update`` whose ``message`` is
    ``None`` on certain edited / inline-mode dispatches. The handler
    MUST return silently — replying via ``None.reply_text`` would
    raise an AttributeError and surface as a Sentry error.
    """
    client = AsyncMock()
    update = _FakeUpdate(message=None, effective_user=_FakeUser(id=12345))
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    client.get_board.assert_not_called()
    client.get_me.assert_not_called()


async def test_board_command_short_circuits_when_user_missing() -> None:
    """Symmetric to the message-missing case: no ``effective_user``
    means we have no ``tg_user_id`` to thread into ``get_me``."""
    client = AsyncMock()
    update = _FakeUpdate(message=_FakeMessage(), effective_user=None)
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    client.get_board.assert_not_called()
    client.get_me.assert_not_called()


async def test_board_command_temporary_error_on_transport_failure() -> None:
    """Non-``MerlinHTTPError`` exceptions (network, ssl, dns, ...) on
    ``get_board`` fall into the broader ``except Exception`` handler
    and still produce the recoverable temporary-error copy — never a
    raw stacktrace into the user's chat.
    """
    client = AsyncMock()
    client.get_board.side_effect = RuntimeError("dns resolution failed")
    client.get_me.return_value = {"display_name": "Alice", "calls_resolved_total": 5}
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try" in body.lower() or "moment" in body.lower()
    # No traceback / stacktrace bleed.
    assert "RuntimeError" not in body
    assert "dns" not in body.lower()


async def test_board_command_temporary_error_on_5xx() -> None:
    client = AsyncMock()
    client.get_board.side_effect = MerlinHTTPError(503, "down")
    client.get_me.return_value = {"display_name": "Alice"}
    update = _FakeUpdate(
        message=_FakeMessage(),
        effective_user=_FakeUser(id=12345),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "try" in body.lower() or "moment" in body.lower()
    assert "503" not in body


# ---------------------------------------------------------------------------
# Build_handler + registration
# ---------------------------------------------------------------------------


def test_build_handler_returns_command_handler_for_board() -> None:
    handler = build_handler()
    assert isinstance(handler, CommandHandler)
    assert "board" in handler.commands


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")
def test_board_handler_registered_in_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot's ``build_application`` must include the ``/board``
    handler — otherwise the slash menu lies."""
    from merlin_bot.application import build_application
    from merlin_bot.config import Settings

    settings = Settings(
        telegram_bot_token="test-token",
        merlin_api_base_url="http://test",
        merlin_api_internal_token="test-token",
    )
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
    assert "board" in commands_registered


# ---------------------------------------------------------------------------
# Horizon filter — keyboard, header, callback round-trip
# ---------------------------------------------------------------------------


def _kb_labels(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Flatten a keyboard's button labels (any row layout) for easy assertion."""
    return [btn.text for row in keyboard.inline_keyboard for btn in row]


def _kb_callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Flatten a keyboard's callback_data values (any row layout)."""
    return [str(btn.callback_data) for row in keyboard.inline_keyboard for btn in row]


@pytest.mark.parametrize(
    "active, expected_labels, expected_callbacks",
    [
        (
            "all",
            ["Day Traders", "Swing Traders", "Position Traders"],
            [
                CB_BOARD_FILTER_DAY,
                CB_BOARD_FILTER_SWING,
                CB_BOARD_FILTER_POSITION,
            ],
        ),
        (
            "day",
            ["All", "Swing Traders", "Position Traders"],
            [
                CB_BOARD_FILTER_ALL,
                CB_BOARD_FILTER_SWING,
                CB_BOARD_FILTER_POSITION,
            ],
        ),
        (
            "swing",
            ["All", "Day Traders", "Position Traders"],
            [
                CB_BOARD_FILTER_ALL,
                CB_BOARD_FILTER_DAY,
                CB_BOARD_FILTER_POSITION,
            ],
        ),
        (
            "position",
            ["All", "Day Traders", "Swing Traders"],
            [
                CB_BOARD_FILTER_ALL,
                CB_BOARD_FILTER_DAY,
                CB_BOARD_FILTER_SWING,
            ],
        ),
    ],
)
def test_filter_keyboard_shows_three_non_active_buttons(
    active: str,
    expected_labels: list[str],
    expected_callbacks: list[str],
) -> None:
    """4 modes, 3 buttons: the active filter is always omitted.

    Pinned because the entire UX rule lives in this one helper — if
    the loop ever degrades to "show all four", the active filter
    would still be tappable and the header text would lie about the
    current slice. Conversely, if the loop ever drops to two non-
    active buttons the user loses a way to reach one of the slices
    via a single tap.
    """
    keyboard = _filter_keyboard(active)
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert _kb_labels(keyboard) == expected_labels
    assert _kb_callbacks(keyboard) == expected_callbacks


def test_filter_keyboard_button_order_is_stable_across_renders() -> None:
    """Filter-button order doesn't change between renders.

    The three non-active buttons always appear in the canonical
    ``all → day → swing → position`` order; the active one is
    dropped from that list. Stable layout avoids the "where did
    that button go" UX surprise.
    """
    # Active "day" → ["All", "Swing Traders", "Position Traders"]
    kb_day = _filter_keyboard("day")
    assert _kb_labels(kb_day) == ["All", "Swing Traders", "Position Traders"]
    # Active "swing" → ["All", "Day Traders", "Position Traders"]
    kb_swing = _filter_keyboard("swing")
    assert _kb_labels(kb_swing) == ["All", "Day Traders", "Position Traders"]
    # Active "position" → ["All", "Day Traders", "Swing Traders"]
    kb_position = _filter_keyboard("position")
    assert _kb_labels(kb_position) == ["All", "Day Traders", "Swing Traders"]


@pytest.mark.parametrize("active", ["all", "day", "swing", "position"])
def test_filter_keyboard_renders_buttons_vertically_one_per_row(
    active: str,
) -> None:
    """Layout regression: each filter button gets its own row.

    "Position Traders" + "Swing Traders" + "All" side-by-side overflows
    on narrow mobile Telegram clients (Telegram truncates rather than
    wraps inline-keyboard rows), so we stack the three non-active
    buttons vertically. Pinned here so a future "compact it back to
    one row" refactor fails this test before reaching users.
    """
    keyboard = _filter_keyboard(active)
    rows = keyboard.inline_keyboard
    assert len(rows) == 3, f"expected 3 rows for active={active!r}, got {len(rows)}"
    for row_idx, row in enumerate(rows):
        assert len(row) == 1, (
            f"row {row_idx} should hold exactly one button (vertical layout); "
            f"got {len(row)} buttons"
        )


@pytest.mark.parametrize(
    "active, expected_suffix",
    [
        ("all", ""),
        ("day", " · Day Traders (≤24h)"),
        ("swing", " · Swing Traders (1d-3wk)"),
        ("position", " · Position Traders (>3wk)"),
    ],
)
def test_render_board_header_reflects_active_filter(
    active: str,
    expected_suffix: str,
) -> None:
    """The header line carries the human-readable filter label.

    Pins the "≤24h" / "1d-3wk" / ">3wk" disclosure on the header —
    the buttons themselves stay short, the header tells users the
    precise boundary so the segmentation is honest.
    """
    payload = {
        "window": "all_time",
        "horizon": active,
        "entries": [_row_entry(rank=1, name="Alice")],
    }
    text = _render_board(payload, active_filter=active)
    first_line = text.splitlines()[0]
    assert first_line == f"<b>Leaderboard</b>{expected_suffix}"


def test_empty_state_text_for_day_mentions_other_three_filters() -> None:
    """Filtered empty state names the empty slice + nudges to switch.

    A day-1 reality is that the unfiltered board has data but the
    ≤24h slice is empty (most users place multi-day calls). The
    empty-state must say WHICH slice is empty so users don't think
    the whole product broke, and the nudge points at every other
    filter so the user has a one-tap route to whatever IS populated.
    """
    text = _empty_state_text(active_filter="day")
    assert "Day Trader" in text
    assert "≤24h" in text
    assert "Swing Traders" in text
    assert "Position Traders" in text
    assert "All" in text


def test_empty_state_text_for_swing_mentions_other_three_filters() -> None:
    text = _empty_state_text(active_filter="swing")
    assert "Swing Trader" in text
    assert "1d-3wk" in text
    assert "Day Traders" in text
    assert "Position Traders" in text
    assert "All" in text


def test_empty_state_text_for_position_explains_maturation_window() -> None:
    """Day-1 reality: zero >3wk resolutions exist yet.

    The position bucket's empty-state is the most-rendered filter-
    empty path on launch, so its copy gets an extra sentence
    explaining WHY it's empty (bets take 3+ weeks to mature) — that
    framing keeps users from reading the gap as a missing feature.
    """
    text = _empty_state_text(active_filter="position")
    assert "Position Trader" in text
    assert ">3wk" in text
    # Day-1 framing: explains the structural reason for the empty
    # bucket, not just "no data yet".
    assert "3+ weeks" in text or "3 weeks" in text
    # Nudge points at the three other filters by name.
    assert "Day Traders" in text
    assert "Swing Traders" in text
    assert "All" in text


def test_empty_state_text_for_all_is_the_original_copy() -> None:
    """Default empty state is unchanged — no regression for clients
    that never specify a filter."""
    text = _empty_state_text()
    assert "Leaderboard unlocks" in text


def test_render_board_empty_position_slice_uses_filtered_copy() -> None:
    """Empty + filter active → the filter-specific empty state fires.

    Uses the ``position`` bucket because that's the realistic
    day-one path (the user has zero >3wk resolved bets), so this
    test is also a smoke-check for the load-bearing copy.
    """
    text = _render_board(
        {"entries": [], "window": "all_time", "horizon": "position"},
        active_filter="position",
    )
    assert "Position Trader" in text
    assert ">3wk" in text


# --- Callback handler round-trip ------------------------------------------


@dataclass
class _FakeQuery:
    data: str
    answered: list[dict[str, Any]] = field(default_factory=list)
    edited: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    edit_side_effect: Exception | None = None

    async def answer(self, **kw: Any) -> None:
        self.answered.append(kw)

    async def edit_message_text(self, text: str, **kw: Any) -> None:
        if self.edit_side_effect is not None:
            exc = self.edit_side_effect
            self.edit_side_effect = None
            raise exc
        self.edited.append((text, kw))


@dataclass
class _FakeChat:
    id: int = 99


@dataclass
class _FakeUpdateWithQuery:
    callback_query: _FakeQuery | None
    effective_user: _FakeUser | None = None
    effective_chat: _FakeChat | None = None


@dataclass
class _FakeBot:
    sent: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent.append((chat_id, text, kw))


@dataclass
class _FakeContextWithBot:
    bot_data: dict[str, Any]
    bot: _FakeBot = field(default_factory=_FakeBot)


@pytest.mark.parametrize(
    "callback_data, expected_filter, expected_header_token",
    [
        (CB_BOARD_FILTER_ALL, "all", ""),
        (CB_BOARD_FILTER_DAY, "day", "Day Traders"),
        (CB_BOARD_FILTER_SWING, "swing", "Swing Traders"),
        (CB_BOARD_FILTER_POSITION, "position", "Position Traders"),
    ],
)
async def test_on_filter_callback_refetches_with_chosen_filter(
    callback_data: str,
    expected_filter: str,
    expected_header_token: str,
) -> None:
    """Tapping a filter button re-fetches /board with the new horizon
    and edits the message in place with the matching keyboard.

    Pins the full callback round-trip: parse → fetch → edit. A bug
    in the prefix-strip would either send the wrong filter to the
    API or fail to find a valid one and bail silently. Parametrized
    across the four modes so every callback constant exercises the
    happy path.
    """
    client = AsyncMock()
    # The API echoes the requested horizon back; the bot trusts the
    # echo to render the header.
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
        "horizon": expected_filter,
    }
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 5,
    }

    query = _FakeQuery(data=callback_data)
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})

    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    client.get_board.assert_awaited_once_with(
        window="all_time",
        horizon=expected_filter,
    )
    assert len(query.edited) == 1
    edited_text, edited_kwargs = query.edited[0]
    first_line = edited_text.splitlines()[0]
    if expected_filter == "all":
        assert first_line == "<b>Leaderboard</b>"
    else:
        assert expected_header_token in first_line
    # The reply keyboard is updated to drop the now-active filter's
    # button — i.e. the three non-active filters are presented.
    keyboard = edited_kwargs["reply_markup"]
    labels = _kb_labels(keyboard)
    assert len(labels) == 3
    active_label = {
        "all": "All",
        "day": "Day Traders",
        "swing": "Swing Traders",
        "position": "Position Traders",
    }[expected_filter]
    assert active_label not in labels


async def test_on_filter_callback_swallows_message_not_modified() -> None:
    """A race that lands "message is not modified" must NOT raise.

    Telegram returns this when the new text/keyboard match the
    existing message exactly. The user's spinner has already cleared
    via ``query.answer()`` — re-raising would surface a Sentry
    error for what is fundamentally a no-op.
    """
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
        "horizon": "all",
    }
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 5,
    }
    query = _FakeQuery(data=CB_BOARD_FILTER_ALL)
    query.edit_side_effect = BadRequest("Message is not modified")
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    bot = _FakeBot()
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client}, bot=bot)

    # Must not raise.
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    # And must NOT fall back to send_message — the existing card is
    # already correct, we just couldn't edit it because it was identical.
    assert bot.sent == []


async def test_on_filter_callback_falls_back_to_send_message_on_other_badrequest() -> None:
    """Non-"not modified" BadRequest (e.g. message too old) re-sends.

    Telegram drops edit rights after 48h. When that fires we must
    deliver the user the refreshed board via a new message rather
    than silently losing the tap.
    """
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
        "horizon": "day",
    }
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 5,
    }
    query = _FakeQuery(data=CB_BOARD_FILTER_DAY)
    query.edit_side_effect = BadRequest("message can't be edited")
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    bot = _FakeBot()
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    assert len(bot.sent) == 1
    sent_chat_id, sent_text, sent_kwargs = bot.sent[0]
    assert sent_chat_id == 99
    assert "Day Traders" in sent_text.splitlines()[0]
    assert sent_kwargs.get("reply_markup") is not None


async def test_on_filter_callback_ignores_malformed_callback_data() -> None:
    """A callback whose data slips past the regex isn't passed to /board.

    Defends against a future regex weakening that lets unrelated
    callbacks reach this handler — we must never forward an unknown
    bucket name to the API.
    """
    client = AsyncMock()
    query = _FakeQuery(data="board:filter:medium")
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})

    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    # query.answer was still called (spinner cleared) but no API
    # call and no edit/send.
    assert query.answered  # spinner cleared
    client.get_board.assert_not_called()
    assert query.edited == []


def test_build_callback_handler_pattern_matches_each_filter() -> None:
    """The registered regex must accept all four filter values and
    reject anything else — including the renamed-away ``short`` /
    ``long`` keys so an old client tapping a stale button gets
    cleanly ignored at the dispatcher layer."""
    import re

    handler = build_callback_handler()
    assert isinstance(handler, CallbackQueryHandler)
    pattern = handler.pattern
    assert pattern is not None
    # ``pattern`` may be a str or a compiled regex; coerce for assertion.
    regex = re.compile(pattern) if isinstance(pattern, str) else pattern
    assert regex.match(CB_BOARD_FILTER_ALL) is not None
    assert regex.match(CB_BOARD_FILTER_DAY) is not None
    assert regex.match(CB_BOARD_FILTER_SWING) is not None
    assert regex.match(CB_BOARD_FILTER_POSITION) is not None
    # Regression: legacy callback_data strings from the 2-bucket
    # ship must be rejected so an old in-flight message is a no-op
    # rather than firing the handler with an unknown bucket name.
    assert regex.match("board:filter:short") is None
    assert regex.match("board:filter:long") is None
    assert regex.match("board:filter:medium") is None
    assert regex.match("history:page:0:0") is None


async def test_on_filter_callback_short_circuits_when_callback_query_missing() -> None:
    """Defensive: an Update with no ``callback_query`` must return
    silently — not crash, not call the API, not touch ``query``."""
    client = AsyncMock()
    update = _FakeUpdateWithQuery(
        callback_query=None,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]
    client.get_board.assert_not_called()


async def test_on_filter_callback_short_circuits_when_query_data_missing() -> None:
    """A callback query with ``data=None`` (rare but possible for
    legacy clients) MUST return silently rather than try to parse
    the missing payload."""
    client = AsyncMock()
    query = _FakeQuery(data=None)  # type: ignore[arg-type]
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]
    client.get_board.assert_not_called()
    assert query.answered == []  # no spinner to clear when we never engaged


async def test_on_filter_callback_short_circuits_when_user_missing() -> None:
    """``effective_user is None`` (channel posts, inline mode, ...)
    means we have no ``tg_user_id`` to thread into /me; bail before
    touching the API."""
    client = AsyncMock()
    query = _FakeQuery(data=CB_BOARD_FILTER_ALL)
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=None,
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]
    client.get_board.assert_not_called()


async def test_on_filter_callback_malformed_data_clears_spinner() -> None:
    """A regex-evading callback (defense in depth) must still call
    ``query.answer()`` so the tap doesn't hang for 30s.

    Pairs with ``test_on_filter_callback_ignores_malformed_callback_data``
    — that test pins the no-API-call behavior, this one pins the
    spinner clear. Together they document the malformed-callback
    contract.
    """
    client = AsyncMock()
    query = _FakeQuery(data="board:filter:medium")
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client})
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]
    assert len(query.answered) == 1  # spinner cleared exactly once
    # The clear is a bare answer() — no toast text — because there's
    # no user-actionable message for a malformed callback.
    assert query.answered[0] == {}


async def test_on_filter_callback_shows_toast_when_fetch_fails() -> None:
    """``get_board`` failing during a callback must surface a toast.

    Telegram only honours ONE ``answerCallbackQuery`` per query, so
    the toast call has to be the only answer call on this path. If
    the handler ever calls ``answer()`` before the toast, the user
    will see no feedback and silently retry. We pin both: the
    message stays intact (no edit, no send) and the toast lands.
    """
    client = AsyncMock()
    client.get_board.side_effect = MerlinHTTPError(503, "down")
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 5,
    }
    query = _FakeQuery(data=CB_BOARD_FILTER_DAY)
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=_FakeChat(id=99),
    )
    bot = _FakeBot()
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    # The toast is the ONLY answer call on this path.
    assert len(query.answered) == 1
    toast_kwargs = query.answered[0]
    assert "try again" in (toast_kwargs.get("text") or "").lower()
    assert toast_kwargs.get("show_alert") is False
    # Message stays intact — no edit attempt, no fallback send.
    assert query.edited == []
    assert bot.sent == []


async def test_on_filter_callback_fallback_skipped_when_no_effective_chat() -> None:
    """The ``send_message`` fallback needs an ``effective_chat`` to
    target. If PTB hands us an Update without one (extreme edge
    case — typically only happens for some inline dispatches), we
    log the failed edit but don't crash on a None chat_id.

    Pins the ``if update.effective_chat is not None`` guard.
    Without it the bot would raise ``AttributeError: 'NoneType'``
    and surface as a Sentry error for a corner case that already
    failed silently from the user's POV (edit raised, no chat).
    """
    client = AsyncMock()
    client.get_board.return_value = {
        "entries": [_row_entry(rank=1, name="Alice")],
        "window": "all_time",
        "horizon": "swing",
    }
    client.get_me.return_value = {
        "display_name": "Alice",
        "calls_resolved_total": 5,
    }
    query = _FakeQuery(data=CB_BOARD_FILTER_SWING)
    query.edit_side_effect = BadRequest("message can't be edited")
    update = _FakeUpdateWithQuery(
        callback_query=query,
        effective_user=_FakeUser(id=42),
        effective_chat=None,  # ← the branch under test
    )
    bot = _FakeBot()
    ctx = _FakeContextWithBot(bot_data={BOT_DATA_KEY: client}, bot=bot)

    # Must not raise.
    await on_filter_callback(update, ctx)  # type: ignore[arg-type]

    assert bot.sent == []  # the guard prevented the fallback send


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")
def test_board_callback_handler_registered_in_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_application`` must include the board filter callback.

    Otherwise tapping a filter button would do nothing — the spinner
    would clear via PTB's default but the bot would never react.
    """
    from merlin_bot.application import build_application
    from merlin_bot.config import Settings

    settings = Settings(
        telegram_bot_token="test-token",
        merlin_api_base_url="http://test",
        merlin_api_internal_token="test-token",
    )
    monkeypatch.setattr(
        "merlin_bot.api_dependency.attach_client",
        lambda *_a, **_kw: None,
    )
    application = build_application(settings)
    patterns: list[str] = []
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CallbackQueryHandler):
                pattern = handler.pattern
                if pattern is not None:
                    patterns.append(
                        pattern if isinstance(pattern, str) else pattern.pattern,
                    )
    assert any("board:filter:" in p for p in patterns), (
        f"board filter CallbackQueryHandler not registered; saw {patterns}"
    )
