"""``/history`` handler — resolved-call history, newest first, paginated.

First page (30 rows) ships on ``/history``. When the API reports
``has_more=True``, an inline "Show 30 more" button is appended; the
button's callback fetches the next page, edits the existing message
to extend the list, and re-renders (or drops) the button with the
new offset.

Bot-voice rules (plan §6):

* Plain-text rows with the same outcome emojis the live resolution
  notifications use — won (🎉), flat (📝), lost (🙂‍↔️). The
  won/lost/flat decision is server-side (``HistoryEntry.is_correct``
  + ``HistoryEntry.is_flat``) so this list and the live ping can't
  drift on which call counted as "right".
* No internal scoring nouns. No "rank", no "shrinkage", no version
  markers.
* Calendar dates only (``Apr 28``, with year on out-of-current-year
  rows) — never raw ISO timestamps; never seconds-precision. The
  live ping already gave the user the exact moment.

Stateless: pagination state lives in the inline-button callback data
(``history:page:<as_of_epoch>:<offset>``), not in ``user_data`` or
persistence. A click on a stale button two days later still works —
``as_of`` freezes the view to the wall-clock of the first request,
so an outcome resolving between page 1 and the "Show 30 more" click
never shifts already-paged rows downward (which would dup the
boundary row).

Long-list handling: each "Show 30 more" click appends rows to the
existing message. Past ~60-90 rows the rendered body approaches
Telegram's 4096-char message cap; on ``BadRequest`` from
``edit_message_text`` we fall back to sending a fresh reply with the
new page (and drop the keyboard from the prior message) so the user
keeps reading instead of hitting a silent button.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from merlin_core.contracts.history import HISTORY_DEFAULT_PAGE_SIZE
from merlin_core.contracts.horizons import humanise_horizon
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.history")

#: Rows per page. Pulled from ``merlin_core.contracts.history`` so the
#: bot's request shape, the API's default, and the button label stay
#: in sync — changing the constant in one place updates all three.
HISTORY_PAGE_SIZE: int = HISTORY_DEFAULT_PAGE_SIZE

#: Callback-data prefix for the "Show 30 more" inline button. Pattern:
#: ``history:page:<as_of_epoch_seconds>:<offset>``. Encoding pagination
#: state in callback_data keeps the bot stateless. The pattern handler
#: validates the integer shape so a stale click can never inject a
#: forged offset or a negative ``as_of``.
_HISTORY_PAGE_CB_PREFIX: str = "history:page:"
_HISTORY_PAGE_CB_PATTERN: str = rf"^{_HISTORY_PAGE_CB_PREFIX}\d+:\d+$"


def _encode_page_callback(*, as_of: datetime, offset: int) -> str:
    """Render the inline-button callback data for the next page.

    Format: ``history:page:<as_of_epoch_seconds>:<offset>``. Epoch
    seconds (not ISO) keeps the payload short — Telegram caps
    ``callback_data`` at 64 bytes. With a 10-digit epoch and a small
    offset, we sit comfortably under 30 chars.

    Second-precision on ``as_of`` is fine: the window ceiling is just
    a "what was 'now' when the user typed /history" anchor; the page
    boundaries are deterministic from the (source, status, as_of)
    triple, and the worker writes outcomes one at a time so two
    outcomes resolving in the same second is implausible.
    """
    if offset < 0:
        raise ValueError(f"offset must be non-negative; got {offset}")
    epoch = int(as_of.timestamp())
    if epoch < 0:
        raise ValueError(f"as_of must be after the unix epoch; got {as_of!r}")
    return f"{_HISTORY_PAGE_CB_PREFIX}{epoch}:{offset}"


def _decode_page_callback(data: str) -> tuple[datetime, int]:
    """Parse ``(as_of, offset)`` out of a ``history:page:...`` callback.

    The pattern matcher already guarantees the shape — this helper
    is a slice + two ``int()`` casts. Raises ``ValueError`` on a
    payload that somehow bypassed the pattern (defence in depth).
    """
    if not data.startswith(_HISTORY_PAGE_CB_PREFIX):
        raise ValueError(f"unexpected callback data: {data!r}")
    suffix = data[len(_HISTORY_PAGE_CB_PREFIX) :]
    epoch_str, offset_str = suffix.split(":", maxsplit=1)
    as_of = datetime.fromtimestamp(int(epoch_str), tz=UTC)
    offset = int(offset_str)
    return as_of, offset


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hit ``GET /me/history`` for the first page and reply."""
    if update.message is None or update.effective_user is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        payload = await client.get_my_history(
            tg_user_id=update.effective_user.id,
            limit=HISTORY_PAGE_SIZE,
            offset=0,
        )
    except MerlinHTTPError as e:
        if e.status_code == 404:
            await update.message.reply_text(_not_onboarded_text())
            return
        _log.warning("get_my_history failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return
    except Exception as e:
        _log.warning("get_my_history transport error: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    text, keyboard = _render_history(payload, prior_text=None)
    await update.message.reply_text(text, reply_markup=keyboard)


async def on_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a "Show 30 more" inline-button press.

    Three steps:

    1. Validate the callback data. On a malformed payload (shouldn't
       happen — the pattern handler gates the shape — but defence in
       depth), ACK with an error toast and bail.
    2. Fetch the next page from the API using the frozen ``as_of``.
       Failures ACK with an error toast and leave the existing
       rows + button alone — the user can retry without losing
       context.
    3. Edit the original message in place: append the new rows and
       re-render the button with the next offset (or drop it
       entirely when no more pages remain). If the edit fails with
       ``Message_too_long`` (we crossed Telegram's 4096-char cap on
       accumulated rows), drop the keyboard from the prior message
       and send a fresh reply with just the new page so the user
       keeps reading.

    Note: ``query.answer()`` can only be called once per callback —
    Telegram invalidates the query id on the first reply. We ACK
    after the success-path edit (or with the error text on failure)
    so the spinner never spins forever AND the toast actually shows.
    """
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return

    try:
        as_of, offset = _decode_page_callback(query.data)
    except ValueError:
        _log.warning("history page: malformed callback data %r", query.data)
        await query.answer(_temporary_error_text(), show_alert=True)
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        payload = await client.get_my_history(
            tg_user_id=update.effective_user.id,
            limit=HISTORY_PAGE_SIZE,
            offset=offset,
            as_of=_as_of_iso(as_of),
        )
    except MerlinHTTPError as e:
        _log.warning("history page %d failed: %s", offset, e)
        await query.answer(_temporary_error_text(), show_alert=True)
        return
    except Exception as e:
        _log.warning("history page %d transport error: %s", offset, e)
        await query.answer(_temporary_error_text(), show_alert=True)
        return

    chat_id = update.effective_chat.id if update.effective_chat is not None else None

    if query.message is None:
        # Original message is gone (rare: the user deleted it) — we
        # can't edit. ACK and send a fresh reply so the user keeps
        # reading instead of staring at a dead spinner. ``chat_id``
        # is typically also None on this branch (PTB derives
        # ``effective_chat`` from ``callback_query.message``); we
        # silently no-op only when both are missing because there's
        # genuinely nowhere to send the reply.
        await query.answer()
        if chat_id is not None:
            fresh_text, fresh_kb = _render_history(payload, prior_text=None)
            await context.bot.send_message(chat_id, fresh_text, reply_markup=fresh_kb)
        return

    # ``query.message`` is typed ``MaybeInaccessibleMessage`` in PTB
    # v21 — duck-type on ``.text`` because only a fully-accessible
    # ``Message`` carries the prior body we need to extend in place.
    prior_text_attr = getattr(query.message, "text", None)
    prior_text: str | None = prior_text_attr if isinstance(prior_text_attr, str) else None
    new_text, keyboard = _render_history(payload, prior_text=prior_text)

    try:
        await query.edit_message_text(new_text, reply_markup=keyboard)
        await query.answer()
    except BadRequest as e:
        # Most common cause: ``Message_too_long`` — accumulated rows
        # crossed Telegram's 4096-char cap. Fall back to sending a
        # fresh reply with just the new page and drop the keyboard
        # from the prior message so the user can keep paginating
        # without the silent failure.
        if _is_message_too_long(e):
            _log.info("history: message too long, falling back to fresh reply")
            await query.answer()
            # Strip the keyboard off the prior message so the user
            # doesn't see two active "Show 30 more" buttons. Best-
            # effort: the prior message may already be in a state
            # where its markup can't be edited (e.g. forwarded).
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            if chat_id is not None:
                fresh_text, fresh_kb = _render_history(payload, prior_text=None)
                await context.bot.send_message(chat_id, fresh_text, reply_markup=fresh_kb)
            return
        # Some other BadRequest — log + ACK so the spinner stops.
        _log.warning("history edit_message_text failed: %s", e)
        await query.answer(_temporary_error_text(), show_alert=True)


def build_handler() -> CommandHandler:
    """Wire the ``/history`` CommandHandler."""
    return CommandHandler("history", history_command)


def build_callback_handler() -> CallbackQueryHandler:
    """Wire the "Show 30 more" inline-button handler."""
    return CallbackQueryHandler(on_page_callback, pattern=_HISTORY_PAGE_CB_PATTERN)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_history(
    payload: dict[str, Any],
    *,
    prior_text: str | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Compose the reply text + (optional) inline keyboard.

    ``prior_text`` is the message body BEFORE this page landed:

    * ``None`` (first ``/history`` invocation OR fresh-reply
      fallback) → render header + rows + footer from scratch.
      Empty entries → the cold-start copy.
    * Non-None ("Show 30 more" click) → strip the trailing footer
      off the prior text, append the new rows, re-add the footer.
      The append-only model means the user keeps scroll position
      and sees the new rows slide in below their existing list.

    The footer is always the last block ("/alpha to add another,
    /tickers for your per-ticker view."). The "Show 30 more" button
    renders only when ``has_more=True`` AND the wire carries an
    ``as_of`` we can round-trip on the next click.
    """
    entries = payload.get("entries") or []
    has_more = bool(payload.get("has_more"))
    next_offset = payload.get("next_offset")
    as_of_iso = payload.get("as_of")
    as_of = _parse_as_of(as_of_iso)

    if prior_text is None:
        if not entries:
            return _empty_state_text(), None
        body_lines = ["Your resolved calls", ""]
        body_lines.extend(_render_row(e) for e in entries)
    else:
        existing_body = _strip_footer(prior_text)
        new_rows = [_render_row(e) for e in entries]
        body_lines = existing_body.splitlines()
        if new_rows:
            body_lines.extend(new_rows)

    text = "\n".join(body_lines) + "\n\n" + _footer_text()

    keyboard: InlineKeyboardMarkup | None = None
    if has_more and isinstance(next_offset, int) and as_of is not None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Show {HISTORY_PAGE_SIZE} more",
                        callback_data=_encode_page_callback(as_of=as_of, offset=next_offset),
                    ),
                ],
            ],
        )
    return text, keyboard


def _strip_footer(prior_text: str) -> str:
    r"""Remove the trailing footer paragraph from a previously-rendered list.

    The footer is ``_footer_text()``; ``\n\n`` separates it from the
    list above. If we find it at the end of the text, strip it +
    the separator. If it's not there (shouldn't happen in practice,
    but defensive against a future message-edit drift) leave the
    prior text untouched and the new rows will just append below.
    """
    footer = _footer_text()
    marker = "\n\n" + footer
    if prior_text.endswith(marker):
        return prior_text[: -len(marker)]
    return prior_text


def _render_row(entry: dict[str, Any]) -> str:
    """One bullet row: ``🎉 TON — Short over 24 hours ended -7.6% · Apr 28``.

    Emoji dispatch follows the same rules as the live resolution
    notifications (server-side classification via ``is_correct`` +
    ``is_flat`` on the wire shape):

    * Won (``is_correct=True``)   → 🎉
    * Flat (``is_correct=False``,
      ``is_flat=True``)            → 📝
    * Lost (``is_correct=False``,
      ``is_flat=False``)           → 🙂‍↔️

    The ``observed_return_pct`` is rendered with one decimal and a
    signed prefix (``+7.6%`` / ``-7.6%``) — same precision as the
    live notification text. Calendar date is ``Apr 28`` (current
    year) or ``Apr 28, 2025`` (out-of-year): users care about "when
    did this resolve, roughly?", not minute-level precision; the
    live ping already gave them the exact moment.
    """
    asset = str(entry.get("asset_symbol") or "?").upper()
    direction = str(entry.get("direction") or "")
    horizon_seconds = entry.get("horizon_seconds")
    pct = entry.get("observed_return_pct")
    resolved_at = entry.get("resolved_at")
    is_correct = bool(entry.get("is_correct"))
    is_flat = bool(entry.get("is_flat"))

    emoji = _pick_emoji(is_correct=is_correct, is_flat=is_flat)
    direction_phrase = _direction_phrase(direction)
    horizon_text = _horizon_human(horizon_seconds)
    pct_text = _format_pct(pct)
    date_text = _format_short_date(resolved_at)

    return (
        f"{emoji} {asset} — {direction_phrase} over {horizon_text} ended {pct_text} · {date_text}"
    )


def _pick_emoji(*, is_correct: bool, is_flat: bool) -> str:
    """Server-classified emoji dispatch — matches resolution notifications.

    ``(True, True)`` is impossible by construction
    (``is_correct_call`` returns False on flats), but we still treat
    True+True as won (favouring the positive icon over the wash) so
    a future contract drift never silently demotes a real win to a
    flat.
    """
    if is_correct:
        return "🎉"
    if is_flat:
        return "📝"
    return "🙂‍↔️"


def _direction_phrase(direction: str) -> str:
    """``long`` → ``Long``, ``short`` → ``Short``.

    Matches the live resolution template's ``_direction_phrase``
    helper so the two surfaces read identically. Unknown directions
    pass through capitalised for forward-compat.
    """
    if direction == "long":
        return "Long"
    if direction == "short":
        return "Short"
    return direction.capitalize() or "?"


def _horizon_human(value: object) -> str:
    """Render horizon seconds as a human phrase (``24 hours`` / ``2 weeks``).

    Wraps ``merlin_core.contracts.horizons.humanise_horizon`` (the
    same helper the live resolution templates use). Defensive fallback
    on missing / non-numeric values keeps a malformed wire row from
    crashing the message.
    """
    if value is None or value == "":
        return "soon"
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "soon"
    if seconds <= 0:
        return "soon"
    return humanise_horizon(seconds)


def _format_pct(pct: object) -> str:
    """Signed % with one decimal, matching the resolution notifications.

    Em-dash fallback on a non-numeric drift so the row never crashes
    the message — but the wire shape guarantees a float here.
    """
    if pct is None:
        return "—%"
    try:
        amount = float(pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—%"
    return f"{amount:+.1f}%"


def _format_short_date(value: object, *, now: datetime | None = None) -> str:
    """Calendar date as ``Apr 28``; ``Apr 28, 2025`` if not current year.

    Three-letter month abbreviation chosen for compactness — the
    row already carries ticker, direction, horizon and PnL; the
    date is the last column. Year is appended only when the
    resolved-at year differs from the current UTC year so users
    with multi-year history can tell rows apart.

    Falls back to an em-dash on a malformed / missing timestamp.
    """
    if not isinstance(value, str) or not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    if dt.year == current.year:
        return f"{dt:%b} {dt.day}"
    return f"{dt:%b} {dt.day}, {dt.year}"


def _parse_as_of(value: object) -> datetime | None:
    """Parse the wire ``as_of`` ISO string back into a UTC datetime.

    Returns ``None`` on missing / malformed values. The caller then
    suppresses the "Show 30 more" button (no point rendering a
    pagination button if we can't round-trip the window ceiling).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _as_of_iso(value: datetime) -> str:
    """Render an ``as_of`` UTC datetime as the wire ISO string.

    Pydantic on the API side accepts the ``Z`` suffix; we emit that
    form for compactness and because it's what the live ping uses
    on the resolution templates.
    """
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_message_too_long(error: BadRequest) -> bool:
    """True if a ``BadRequest`` from ``edit_message_text`` is a length cap.

    Telegram's message body cap is 4096 chars; cross it and the API
    returns ``Bad Request: message is too long``. PTB surfaces the
    raw message text; we match case-insensitively so a minor wording
    change on Telegram's side doesn't break the fallback path.
    """
    return "too long" in str(error).lower()


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _footer_text() -> str:
    """Footer call-to-action — same paragraph for first page and follow-ups."""
    return "/alpha to add another, /tickers for your per-ticker view."


def _empty_state_text() -> str:
    """Cold-start: no resolved calls yet."""
    return (
        "No resolved calls yet. After your first one lands, you'll "
        "see the play-by-play here.\n"
        "\n"
        "/alpha to add a call."
    )


def _not_onboarded_text() -> str:
    """User typed /history but never finished /start."""
    return "Hit /start first to set up your account."


def _temporary_error_text() -> str:
    """API down or unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /history again in a moment."
