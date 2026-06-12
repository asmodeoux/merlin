"""``/tickers`` handler — per-ticker breakdown of the caller's resolved calls.

Stateless. Hits ``GET /me/tickers?tg_user_id=...`` and renders each
ticker on its own bullet row using the same chunk order as the
leaderboard (PnL primary, hit rate secondary, call count).

Bot-voice rules (plan §6):

* Honest empty-state. Zero resolved calls reads "Tracking your first
  resolutions" rather than a blank list — the API only returns
  tickers with at least one resolved bet, so the empty list ONLY
  fires on a cold-start source.
* No internal scoring nouns ("Bayesian", "shrinkage", "regret",
  "belief score") on the rendered string, even when populated.
* Plain text — no MarkdownV2 escape gauntlet. The bullet glyph and
  middle-dot separator are not Markdown specials, so the message
  ships without ``parse_mode``.
"""

from __future__ import annotations

import logging
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot import rich
from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.tickers")

#: Bullet glyph for each per-ticker row. U+2022 (Unicode BULLET) —
#: same character ``/me`` uses for its open-call list, so the two
#: surfaces share a visual rhythm. Not a Markdown special char so it
#: ships without ``parse_mode``.
_TICKER_BULLET = "\u2022"


async def tickers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hit ``GET /me/tickers`` and reply with the rendered breakdown."""
    if update.message is None or update.effective_user is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        payload = await client.get_my_tickers(tg_user_id=update.effective_user.id)
    except MerlinHTTPError as e:
        if e.status_code == 404:
            await update.message.reply_text(_not_onboarded_text())
            return
        _log.warning("get_my_tickers failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return
    except Exception as e:
        _log.warning("get_my_tickers transport error: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    await rich.send_rich(
        context.bot,
        update.message.chat_id,
        _render_tickers_rich(payload),
        fallback_text=_render_tickers(payload),
    )


def build_handler() -> CommandHandler:
    """Wire the ``/tickers`` CommandHandler."""
    return CommandHandler("tickers", tickers_command)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_tickers(payload: dict[str, Any]) -> str:
    """Compose the ``/tickers`` reply.

    Layout::

        Your tickers
        Sorted by track-record-weighted PnL.

        • TON — PnL +6.1% · 50% hit · 2 calls
        • BTC — PnL -1.3% · 0% hit · 1 call

        /alpha to add another, /history for past calls.

    The caption explains the ordering in plain English (no scoring
    nouns) so a +20% (1-call) row sitting below a +6% (4-call) row
    doesn't read as random.

    Empty ``entries`` → the honest cold-start copy. Populated → one
    bullet per ticker, in server-side sorted order (Bayesian-shrunk
    PnL, leaderboard tie-breakers). The bot does not re-sort; the
    server's order is the contract.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text()

    lines = [
        "Your tickers",
        "Sorted by track-record-weighted PnL.",
        "",
    ]
    for entry in entries:
        lines.append(_render_row(entry))
    lines.append("")
    lines.append("/alpha to add another, /history for past calls.")
    return "\n".join(lines)


def _render_tickers_rich(payload: dict[str, Any]) -> str:
    """Compose the ``/tickers`` reply as a Rich Message (Bot API 10.1).

    Same data + sort order as :func:`_render_tickers`; the per-ticker
    bullet list becomes a native four-column table so PnL / hit / calls
    line up in their own right-aligned columns instead of running
    inline behind middle-dots. The cold-start empty state stays plain
    prose (no table to align) — it's one sentence and a CTA.

    Layout::

        # Your tickers
        _Sorted by track-record-weighted PnL._

        | Ticker |  PnL | Hit | Calls |
        |:-------|-----:|----:|------:|
        | TON    | +6.1% | 50% |     2 |
        | BTC    | -1.3% | 25% |     8 |

        /alpha to add another, /history for past calls.

    The asset symbol is the only user-influenced cell, escaped via
    :func:`merlin_bot.rich.escape_cell`; numeric cells are
    renderer-controlled so they need no escaping.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text()

    body = rich.table(
        ["Ticker", "PnL", "Hit", "Calls"],
        [_rich_row(entry) for entry in entries],
        ["left", "right", "right", "right"],
    )
    return "\n".join(
        [
            "# Your tickers",
            "",
            "_Sorted by track-record-weighted PnL._",
            "",
            body,
            "",
            "/alpha to add another, /history for past calls.",
        ],
    )


def _rich_row(entry: dict[str, Any]) -> list[str]:
    """One table row ``[ticker, pnl, hit, calls]`` for the rich view.

    Number formats match the plain renderer's chunks minus their inline
    labels (the column header carries the label): signed one-decimal
    PnL, integer hit-rate percent, bare resolved count. Em-dash on a
    non-numeric wire drift so a single malformed row never breaks the
    whole table.
    """
    asset = rich.escape_cell(str(entry.get("asset_symbol") or "?").upper())
    return [
        asset,
        _pnl_cell(entry.get("weighted_pnl_pct")),
        _hit_cell(entry.get("accuracy_pct")),
        _calls_cell(entry.get("calls_resolved")),
    ]


def _pnl_cell(pnl: object) -> str:
    """Signed one-decimal PnL for a table cell: ``+6.1%`` / ``-1.3%``."""
    if pnl is None:
        return "—"
    try:
        amount = float(pnl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{amount:+.1f}%"


def _hit_cell(accuracy: object) -> str:
    """Integer hit-rate percent for a table cell: ``50%``."""
    if accuracy is None:
        return "—"
    try:
        amount = float(accuracy)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{amount:.0f}%"


def _calls_cell(calls: object) -> str:
    """Bare resolved-count for a table cell: ``2``. Em-dash on drift."""
    if not isinstance(calls, int):
        return "—"
    return str(calls)


def _render_row(entry: dict[str, Any]) -> str:
    """One bullet row: ``• TON — PnL +6.1% · 50% hit · 2 calls``.

    Number formats mirror ``/me`` and ``/board`` so the headline
    numbers are byte-identical across surfaces:

    * PnL: signed, one decimal (``+6.1%`` / ``-1.3%``). One decimal,
      not two — the leaderboard renders two, but the per-ticker view
      shows up to dozens of rows on one screen and two decimals
      crowd the line. The headline is "did this ticker pay?"; a
      tenth of a percent answers it.
    * Hit rate: integer percent (``50%``) — same as ``/board``.
    * Resolved count: pluralised (``1 call`` / ``2 calls``) — same
      as ``/me`` / ``/board``.
    """
    asset = str(entry.get("asset_symbol") or "?").upper()
    pnl = entry.get("weighted_pnl_pct")
    accuracy = entry.get("accuracy_pct")
    calls = entry.get("calls_resolved")

    pnl_chunk = _format_pnl_chunk(pnl)
    hit_chunk = _format_hit_chunk(accuracy)
    calls_chunk = _format_calls_chunk(calls)

    return f"{_TICKER_BULLET} {asset} — {pnl_chunk} · {hit_chunk} · {calls_chunk}"


def _format_pnl_chunk(pnl: object) -> str:
    """Signed PnL with one decimal: ``PnL +6.1%`` / ``PnL -1.3%``.

    The API guarantees a number on every ``TickerEntry`` (empty /
    cold-start tickers are filtered server-side), but the renderer
    still falls back to an em-dash on a non-numeric drift so a
    malformed wire row never crashes the message.
    """
    if pnl is None:
        return "— PnL"
    try:
        amount = float(pnl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— PnL"
    return f"PnL {amount:+.1f}%"


def _format_hit_chunk(accuracy: object) -> str:
    """Integer hit rate: ``50% hit``. Em-dash on None / non-numeric."""
    if accuracy is None:
        return "— hit"
    try:
        amount = float(accuracy)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— hit"
    return f"{amount:.0f}% hit"


def _format_calls_chunk(calls: object) -> str:
    """Pluralised resolved-count: ``1 call`` / ``2 calls``.

    ``calls_resolved`` is a non-Optional ``int`` on the wire (Pydantic
    rejects anything else), so the narrowing here is just a final
    safety net against a future contract drift.
    """
    if not isinstance(calls, int):
        return "— calls"
    suffix = "" if calls == 1 else "s"
    return f"{calls} call{suffix}"


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _empty_state_text() -> str:
    """Cold-start copy for a source with no resolved calls.

    Frames the gap as a milestone they can reach ("after a few calls
    land") rather than a missing feature. No version markers, no
    "soon", no scoring jargon.
    """
    return (
        "Tracking your first resolutions. After a few calls land, "
        "you'll see your per-ticker breakdown here.\n"
        "\n"
        "/alpha to add a call."
    )


def _not_onboarded_text() -> str:
    """User typed /tickers but never finished onboarding."""
    return "Hit /start first to set up your account."


def _temporary_error_text() -> str:
    """API down or unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /tickers again in a moment."
