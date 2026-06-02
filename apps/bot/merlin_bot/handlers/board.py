"""``/board`` handler — the leaderboard.

Stateless. Hits ``GET /board?window=all_time``; renders the top-N as
emoji-prefixed lines (medals for the top three, plain rank numbers
below). The caller's own row is bolded so they can find themselves at
a glance in a packed list.

Self-positioning rules:

* If the caller's rank is inside the visible top-N → their row is
  bolded inline (no extra footer).
* If the caller has resolved calls but ranks outside the top-N → an
  "…" gap line followed by their row is appended so they always see
  themselves on the board.
* If the caller has zero resolved calls → no row appendage; we print
  an inspiring one-liner instead. They're not on the board yet, and
  showing rank "—" or skipping silently both feel worse than telling
  them what unlocks a spot.

Bot-voice rules (plan §6):

* No "v0" / "Phase 2" / "coming soon" markers. The empty-state reads
  "Leaderboard unlocks after the first resolutions." — present tense,
  product framing.
* No internal scoring nouns ("Bayesian", "shrinkage", "regret",
  "belief score") on the rendered string, even in the populated path.
* HTML parse mode — Telegram's ``<b>...</b>`` is the only formatting
  we use (to bold the caller's row). HTML is far gentler on
  user-controlled text than MarkdownV2: we only need to escape the
  five HTML special chars in the display_name + numeric fields rather
  than every MarkdownV2 punctuation mark.

We also fetch ``GET /me`` in parallel with ``GET /board`` to learn the
caller's display_name + resolved-count. The display_name powers self-
recognition (bold + appendage match); the count distinguishes
"cold-start, show the inspiring nudge" from "has resolutions, must be
somewhere in the data". If /me fails the board still renders — just
without self-highlight or appendage. /board is the must-have, /me is
best-effort.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.board")

# How many rows we show on the rendered board. The query may return
# more; we trim client-side to keep the message short. 30 is a
# deliberate balance — long enough that the typical caller is on the
# board without scrolling tricks, short enough that the message fits
# comfortably under Telegram's 4096-char limit even with HTML markup
# (30 * ~80 chars = ~2.4 KB, well clear).
LEADERBOARD_LIMIT: int = 30

# Emoji prefixes for the top three ranks. Anything below the podium
# gets the rank number rendered as plain text (4., 5., ...) so the
# visual hierarchy stays obvious without making the bottom of the
# board feel like an afterthought.
_PODIUM_EMOJI: dict[int, str] = {1: "🥇", 2: "🥈", 3: "🥉"}

# Gap separator between the visible top-N and the caller's row when
# the caller ranks outside the top-N. A single horizontal-ellipsis
# glyph (U+2026), used standalone on its own line — Telegram renders
# it identically across clients and it doesn't need HTML-escaping.
_GAP_LINE = "…"


async def board_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``GET /board`` (+ ``GET /me``) and reply with the rendered top-N.

    /board is the must-have; /me is best-effort because we only use it
    to recognise "this row is the caller, bold it". If /me errors,
    the leaderboard still renders — just without self-highlight.
    """
    if update.message is None or update.effective_user is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    tg_user_id = update.effective_user.id

    board_task = asyncio.create_task(client.get_board(window="all_time"))
    me_task = asyncio.create_task(client.get_me(tg_user_id=tg_user_id))

    try:
        board_payload = await board_task
    except MerlinHTTPError as e:
        _log.warning("get_board failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        # Drain the /me future so we don't leak an "Exception was
        # never retrieved" warning into the worker logs.
        me_task.cancel()
        return
    except Exception as e:
        _log.warning("get_board transport error: %s", e)
        await update.message.reply_text(_temporary_error_text())
        me_task.cancel()
        return

    self_display_name: str | None = None
    self_resolved_total: int = 0
    try:
        me_payload = await me_task
    except Exception as e:
        # /me failures (404, transport, etc.) degrade gracefully:
        # leaderboard renders without bolding the caller's row and
        # without the cold-start / outside-top-N appendage.
        _log.debug("get_me for /board self-highlight failed: %s", e)
    else:
        self_display_name = me_payload.get("display_name")
        # Defensive int-cast: server contract guarantees an int, but a
        # mid-deploy stale schema (None / str) shouldn't crash /board.
        try:
            self_resolved_total = int(me_payload.get("calls_resolved_total") or 0)
        except (TypeError, ValueError):
            self_resolved_total = 0

    await update.message.reply_text(
        _render_board(
            board_payload,
            self_display_name=self_display_name,
            self_resolved_total=self_resolved_total,
        ),
        parse_mode=ParseMode.HTML,
    )


def build_handler() -> CommandHandler:
    """Wire the ``/board`` CommandHandler."""
    return CommandHandler("board", board_command)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_board(
    payload: dict[str, Any],
    *,
    self_display_name: str | None = None,
    self_resolved_total: int = 0,
) -> str:
    """Compose the ``/board`` reply text (HTML).

    Empty entries → the honest empty-state. Non-empty → medal-emoji
    top-3 + plain rank-number rest, with each row carrying:

    * Display name (caller's row wrapped in ``<b>...</b>``).
    * Conviction-weighted PnL %, signed, two decimals — the primary
      headline per the locked ``primary_metric = pnl_primary`` choice.
    * Hit-rate %, no decimals — the secondary number.
    * Resolved-count ("8 calls" / "1 call").

    After the visible top-N (``LEADERBOARD_LIMIT``), one of three
    appendages may follow (see ``_self_appendage_lines``):

    * "…" + caller's row, when they rank outside the top-N.
    * Inspiring one-liner, when they have zero resolved calls.
    * Nothing, when they're already inside the top-N (their row is
      bolded inline) or /me failed.

    A footer demystifies the ranking method in one line so users
    don't wonder why a +10% one-shot ranks mid-pack.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text()

    top_entries = entries[:LEADERBOARD_LIMIT]

    lines = ["<b>Leaderboard</b>"]
    for entry in top_entries:
        lines.append(_render_row(entry, self_display_name=self_display_name))

    lines.extend(
        _self_appendage_lines(
            entries=entries,
            visible_count=len(top_entries),
            self_display_name=self_display_name,
            self_resolved_total=self_resolved_total,
        ),
    )

    lines.append("")
    lines.append("<i>Ranked by recent PnL, weighted by track record.</i>")
    lines.append("/alpha to add a call, /me for your own.")
    return "\n".join(lines)


def _self_appendage_lines(
    *,
    entries: list[dict[str, Any]],
    visible_count: int,
    self_display_name: str | None,
    self_resolved_total: int,
) -> list[str]:
    r"""Build the "…\n{caller's row}" or inspiring-message appendage.

    Three behaviour branches, in priority order:

    1. **/me failed** (``self_display_name is None``): no appendage.
       We don't know who the caller is, so we can't show their row or
       a meaningful nudge.
    2. **Caller in the visible top-N**: no appendage. Their row is
       already bolded above; a second copy below would be redundant.
    3. **Caller outside the visible top-N**, has resolved calls
       (rank exists in ``entries``): show ``"…"`` then their row.
       Lets a rank-42 caller see their position without scrolling
       past 30+ podium rows of unrelated people.
    4. **Caller has zero resolved calls** (``self_resolved_total ==
       0``): inspiring one-liner instead of a row. They aren't on
       the leaderboard at all — the API filters cold-start sources
       — but we tell them what unlocks a spot.

    The "has resolved calls but isn't in ``entries``" combination is
    treated as anomalous (mid-recompute snapshot lag, schema drift)
    and silenced — no appendage — rather than rendered with a
    placeholder row. Loud-fail in logs, not in the user's UI.
    """
    if self_display_name is None:
        return []

    caller_row = _find_caller_row(entries, self_display_name)

    if caller_row is not None:
        caller_rank = _safe_int(caller_row.get("rank"))
        if caller_rank <= visible_count:
            return []
        return [
            _GAP_LINE,
            _render_row(caller_row, self_display_name=self_display_name),
        ]

    if self_resolved_total == 0:
        return ["", _cold_start_message()]

    # Has resolved calls but not in the leaderboard — anomalous, see
    # the docstring above. Log so we can investigate without scaring
    # the caller.
    _log.warning(
        "caller %r has %d resolved calls but is missing from /board",
        self_display_name,
        self_resolved_total,
    )
    return []


def _find_caller_row(
    entries: list[dict[str, Any]],
    self_display_name: str,
) -> dict[str, Any] | None:
    """Locate the caller's row by display_name.

    Display names are user-chosen and not strictly unique, but
    ``Source.display_name`` is capped at 40 chars and collisions are
    rare in practice. A two-Yuri collision would highlight + append
    the first row found, which is mildly wrong but never wrong-data
    — both Yuris would see one of their PnL on the board. Acceptable
    until we surface a stable, non-PII caller-rank field on /me.
    """
    for entry in entries:
        if entry.get("display_name") == self_display_name:
            return entry
    return None


def _safe_int(value: object) -> int:
    """``int(value)`` with a 0 fallback for None / non-numeric.

    Wire row fields land here as ``object`` after ``dict.get`` —
    mypy strict mode rejects a direct ``int(value)`` so we narrow via
    isinstance for ints/floats/strs (the realistic JSON-shape inputs)
    and fall through to 0 for anything else.
    """
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _cold_start_message() -> str:
    """Inspiring one-liner for a caller with zero resolved calls.

    Frames the gap as a milestone they can reach ("lands you on the
    board") rather than a missing feature. No version markers, no
    "soon" / "coming soon", no scoring jargon. The call to action
    (/alpha) is implicit via the footer that follows.
    """
    return "Your first resolution lands you on the board."


def _render_row(
    entry: dict[str, Any],
    *,
    self_display_name: str | None,
) -> str:
    """One leaderboard row in HTML, with optional self-bolding."""
    rank = int(entry.get("rank") or 0)
    raw_name = str(entry.get("display_name") or "anonymous")
    pnl = entry.get("weighted_pnl_pct")
    accuracy = entry.get("accuracy_pct")
    resolved = int(entry.get("calls_resolved") or 0)

    name_escaped = html.escape(raw_name)
    if self_display_name is not None and raw_name == self_display_name:
        # Wrap the name in <b> so the caller can find themselves
        # without scanning the whole list. We bold only the name (not
        # the whole row) so the layout stays uniform.
        name_rendered = f"<b>{name_escaped}</b>"
    else:
        name_rendered = name_escaped

    prefix = _PODIUM_EMOJI.get(rank, f"{rank}.")
    pnl_chunk = _format_pnl_chunk(pnl)
    accuracy_chunk = _format_accuracy_chunk(accuracy)
    calls_chunk = _format_calls_chunk(resolved)

    return f"{prefix} {name_rendered} — {pnl_chunk} · {accuracy_chunk} · {calls_chunk}"


def _format_pnl_chunk(pnl: object) -> str:
    """Signed PnL with two decimals: ``+2.50% PnL`` / ``-1.30% PnL``.

    ``None`` only happens defensively — the API filters cold-start
    sources OUT of the leaderboard already — but we render an em-dash
    rather than crash on a future schema change that lets a NULL
    through. Non-numeric values fall back to the em-dash too so the
    handler never raises on a malformed wire row.
    """
    if pnl is None:
        return "— PnL"
    try:
        amount = float(pnl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— PnL"
    return f"{amount:+.2f}% PnL"


def _format_accuracy_chunk(accuracy: object) -> str:
    """Hit-rate with no decimals: ``50% hit``. Em-dash for None."""
    if accuracy is None:
        return "— hit"
    try:
        amount = float(accuracy)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— hit"
    return f"{amount:.0f}% hit"


def _format_calls_chunk(resolved: int) -> str:
    """Pluralised resolved-count: ``1 call`` / ``8 calls``."""
    suffix = "" if resolved == 1 else "s"
    return f"{resolved} call{suffix}"


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _empty_state_text() -> str:
    """Empty-state copy for when no source has resolved a call yet.

    Frames the gap as a product choice ("unlocks after the first
    resolutions") rather than a missing feature. Avoids version
    markers, internal scoring nouns, and any "soon" / "coming soon"
    language that ages badly.
    """
    return (
        "Leaderboard unlocks after the first resolutions.\n"
        "\n"
        "/alpha to add a call, or /me for your own open ones."
    )


def _temporary_error_text() -> str:
    """API down / unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /board again in a moment."
