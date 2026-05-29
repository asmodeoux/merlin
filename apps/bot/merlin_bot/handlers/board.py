"""``/board`` handler — the leaderboard.

Stateless. Hits ``GET /board?window=all_time``. Today the API
returns an empty list (no outcomes layer yet); we render an honest
empty-state that frames the gap as a product choice rather than a
broken feature. When the worker lands, this handler swaps the
empty-state for the top-N renderer that's already written below —
nothing else moves.

Bot-voice rules (plan §6):

* No "v0" / "Phase 2" / "coming soon" markers. The empty-state reads
  "Leaderboard unlocks after the first resolutions." — present tense,
  product framing.
* No internal scoring nouns ("Bayesian", "shrinkage", "regret",
  "belief score") on the rendered string, even in the populated path.
* Plain-text only — no MarkdownV2 escaping.
"""

from __future__ import annotations

import logging
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.board")

# How many rows we show on the rendered board. The query may return
# more; we trim client-side to keep the message short. Mirrors
# common /leaderboard UIs.
TOP_N: int = 10


async def board_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``GET /board`` and reply with the rendered top-N."""
    if update.message is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        payload = await client.get_board(window="all_time")
    except MerlinHTTPError as e:
        _log.warning("get_board failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return
    except Exception as e:
        _log.warning("get_board transport error: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    await update.message.reply_text(_render_board(payload))


def build_handler() -> CommandHandler:
    """Wire the ``/board`` CommandHandler."""
    return CommandHandler("board", board_command)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_board(payload: dict[str, Any]) -> str:
    """Compose the ``/board`` reply text.

    Empty entries → the honest empty-state. Non-empty → top-N with
    rank, display name, accuracy, and resolved-count. Both forms share
    the same closing call-to-action so the user always knows where to
    go next.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text()

    lines = ["Leaderboard:"]
    for entry in entries[:TOP_N]:
        rank = int(entry.get("rank") or 0)
        name = str(entry.get("display_name") or "anonymous")
        accuracy = entry.get("accuracy_pct")
        resolved = int(entry.get("calls_resolved") or 0)
        accuracy_str = f"{accuracy:.0f}%" if accuracy is not None else "—"
        lines.append(f"  {rank}. {name} — {accuracy_str} ({resolved} resolved)")

    lines.append("")
    lines.append("/alpha to add a call, /me for your own.")
    return "\n".join(lines)


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
