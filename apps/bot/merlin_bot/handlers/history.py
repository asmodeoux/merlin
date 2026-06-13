"""``/history`` handler — resolved-call history, newest first, paginated.

First page (30 rows) ships on ``/history`` as a Bot API 10.1 Rich
Message: a native table of resolved calls. When the API reports
``has_more=True``, an inline "Show 30 more" button is attached; the
button's callback re-fetches the accumulated window, edits the existing
message in place to show the full table, and re-renders (or drops) the
button with the new offset.

Bot-voice rules (plan §6):

* Table rows carry the same outcome emojis the live resolution
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

Rich pagination — why re-fetch instead of append: a Rich Message's
source Markdown is NOT recoverable from ``query.message.text`` (the
content lives in the new ``rich_message`` field, which PTB 21.x doesn't
model), so the plain renderer's "strip the footer off the prior body
and append the new rows" trick can't rebuild the table. Instead each
"Show more" tap re-fetches the whole window ``[0, target)`` from the
frozen ``as_of`` and re-renders the complete table — still stateless.
Once the window would exceed ``HISTORY_WINDOW_LIMIT`` (the API's
single-page cap) we degrade to a fresh message carrying just the next
page and drop the prior keyboard — the same graceful fallback the plain
renderer used when accumulated rows crossed Telegram's 4096-char cap.
The legacy plain renderer (:func:`_render_history`) is retained as the
``send_rich`` fallback for clients/servers that can't render rich.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from merlin_core.contracts.history import HISTORY_DEFAULT_PAGE_SIZE, HISTORY_MAX_PAGE_SIZE
from merlin_core.contracts.horizons import humanise_horizon
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from merlin_bot import rich
from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.history")

#: Rows per page. Pulled from ``merlin_core.contracts.history`` so the
#: bot's request shape, the API's default, and the button label stay
#: in sync — changing the constant in one place updates all three.
HISTORY_PAGE_SIZE: int = HISTORY_DEFAULT_PAGE_SIZE

#: Largest single-fetch window. Rich Messages (Bot API 10.1) don't
#: round-trip through ``Message.text``, so the old "append to the prior
#: message body" trick can't reconstruct the table on a "Show more"
#: tap. Instead each tap re-fetches the whole window ``[0, target)`` and
#: re-renders the full table in place — stateless, driven entirely by
#: the callback data. The API caps a single page at
#: ``HISTORY_MAX_PAGE_SIZE``; once the accumulated window would exceed
#: it we degrade to a fresh message carrying just the next page (the
#: same graceful fallback the plain renderer used on the 4096-char cap).
HISTORY_WINDOW_LIMIT: int = HISTORY_MAX_PAGE_SIZE

#: Callback-data prefix for the "Show 30 more" inline button. Pattern:
#: ``history:page:<as_of_epoch_seconds>:<offset>``. Encoding pagination
#: state in callback_data keeps the bot stateless. The pattern handler
#: validates the integer shape so a stale click can never inject a
#: forged offset or a negative ``as_of``.
_HISTORY_PAGE_CB_PREFIX: str = "history:page:"
_HISTORY_PAGE_CB_PATTERN: str = rf"^{_HISTORY_PAGE_CB_PREFIX}\d+:\d+$"

# Substring Telegram returns when an edit's content is byte-identical to
# the live message. A fast double-tap on "Show 30 more" can race two
# clicks carrying the same offset into rendering the same window, so the
# second edit is a no-op — swallow it rather than flashing an error toast.
_NOT_MODIFIED_SUBSTRING: str = "message is not modified"


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

    rich_text, keyboard = _render_history_rich(payload)
    fallback_text, _ = _render_history(payload, prior_text=None)
    await rich.send_rich(
        context.bot,
        update.message.chat_id,
        rich_text,
        reply_markup=keyboard,
        fallback_text=fallback_text,
    )


async def on_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a "Show 30 more" inline-button press.

    Steps:

    1. Validate the callback data. On a malformed payload (shouldn't
       happen — the pattern handler gates the shape — but defence in
       depth), ACK with an error toast and bail.
    2. Re-fetch the accumulated window ``[0, offset + page)`` from the
       frozen ``as_of`` (rich messages can't be reconstructed from the
       prior body — see the module docstring). Failures ACK with an
       error toast and leave the existing rows + button alone.
    3. If the window fits in a single API page, re-render the full
       table and edit the message in place. If it would exceed the
       single-page cap, degrade to a fresh message carrying just the
       next page and drop the prior keyboard.

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
    chat_id = update.effective_chat.id if update.effective_chat is not None else None

    # ``offset`` is the count of rows already shown; the tap wants the
    # next page on top, i.e. the full window up to ``target`` rows.
    target = offset + HISTORY_PAGE_SIZE
    fits_in_one_fetch = target <= HISTORY_WINDOW_LIMIT
    fetch_limit = target if fits_in_one_fetch else HISTORY_PAGE_SIZE
    fetch_offset = 0 if fits_in_one_fetch else offset

    try:
        payload = await client.get_my_history(
            tg_user_id=update.effective_user.id,
            limit=fetch_limit,
            offset=fetch_offset,
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

    new_text, keyboard = _render_history_rich(payload)
    message_id = getattr(query.message, "message_id", None)

    # Past the single-fetch cap, OR when the original message is gone
    # (user deleted it), we can't grow it in place — send a fresh
    # message with this page and stop the prior message's button.
    if not fits_in_one_fetch or message_id is None or chat_id is None:
        await query.answer()
        if message_id is not None:
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
        if chat_id is not None:
            await _send_fresh_page(context, chat_id, payload, new_text, keyboard)
        return

    try:
        await rich.edit_rich(
            context.bot,
            chat_id=chat_id,
            message_id=message_id,
            markdown=new_text,
            reply_markup=keyboard,
        )
        await query.answer()
    except BadRequest as e:
        if _NOT_MODIFIED_SUBSTRING in str(e).lower():
            # Identical re-render (e.g. a fast double-tap on "Show more"
            # before the first edit landed) — clear the spinner, no error.
            await query.answer()
        elif _is_message_too_long(e):
            # The accumulated table crossed a Rich Message limit
            # (32768 chars / 500 blocks). Drop the keyboard and send
            # the window as a fresh message so the user keeps reading.
            _log.info("history: rich message too large, falling back to fresh reply")
            await query.answer()
            with contextlib.suppress(BadRequest):
                await query.edit_message_reply_markup(reply_markup=None)
            await _send_fresh_page(context, chat_id, payload, new_text, keyboard)
        else:
            _log.warning("history edit_rich failed: %s", e)
            await query.answer(_temporary_error_text(), show_alert=True)


async def _send_fresh_page(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    payload: dict[str, Any],
    rich_text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """Send ``payload`` as a fresh Rich Message (pagination fallback).

    Used when the growing message can't be edited in place — the window
    outgrew the single-fetch cap, the original message was deleted, or a
    rich limit was hit. The plain render is the ``send_rich`` fallback.
    """
    fallback_text, _ = _render_history(payload, prior_text=None)
    await rich.send_rich(
        context.bot,
        chat_id,
        rich_text,
        reply_markup=keyboard,
        fallback_text=fallback_text,
    )


def build_handler() -> CommandHandler:
    """Wire the ``/history`` CommandHandler."""
    return CommandHandler("history", history_command)


def build_callback_handler() -> CallbackQueryHandler:
    """Wire the "Show 30 more" inline-button handler."""
    return CallbackQueryHandler(on_page_callback, pattern=_HISTORY_PAGE_CB_PATTERN)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_history_rich(
    payload: dict[str, Any],
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Compose the ``/history`` reply as a Rich Message (Bot API 10.1).

    Renders the FULL window in ``payload`` (the caller re-fetches the
    accumulated rows on each "Show more" tap — see the module docstring)
    as a native six-column table: outcome emoji, asset, direction,
    horizon, return, resolved-date. Same server-side won/flat/lost
    classification and same date/return formatting as the plain
    :func:`_render_row`. The "Show 30 more" button is attached whenever
    the wire reports ``has_more`` and carries a round-trippable
    ``as_of`` + ``next_offset``.

    Empty entries → the plain cold-start copy (no table to align).
    """
    entries = payload.get("entries") or []
    has_more = bool(payload.get("has_more"))
    next_offset = payload.get("next_offset")
    as_of = _parse_as_of(payload.get("as_of"))

    if not entries:
        return _empty_state_text(), None

    lines = [
        "# Your resolved calls",
        "",
        rich.table_header(
            ["", "Asset", "Call", "Horizon", "Return", "Resolved"],
            ["left", "left", "left", "left", "right", "left"],
        ),
    ]
    lines.extend(rich.table_row(_rich_history_row(entry)) for entry in entries)
    text = "\n".join(lines) + "\n\n" + _footer_text()
    return text, _page_keyboard(has_more=has_more, next_offset=next_offset, as_of=as_of)


def _rich_history_row(entry: dict[str, Any]) -> list[str]:
    """One resolved-call table row for the rich view.

    ``[emoji, asset, direction, horizon, return, date]``. Field
    formatting mirrors the plain :func:`_render_row` exactly; the asset,
    direction, horizon, and date cells are escaped via
    :func:`merlin_bot.rich.escape_cell` (the emoji and signed-percent
    cells are renderer-controlled).
    """
    emoji = _pick_emoji(
        is_correct=bool(entry.get("is_correct")),
        is_flat=bool(entry.get("is_flat")),
    )
    asset = rich.escape_cell(str(entry.get("asset_symbol") or "?").upper())
    direction = rich.escape_cell(_direction_phrase(str(entry.get("direction") or "")))
    horizon = rich.escape_cell(_horizon_human(entry.get("horizon_seconds")))
    pct = _format_pct(entry.get("observed_return_pct"))
    date = rich.escape_cell(_format_short_date(entry.get("resolved_at")))
    return [emoji, asset, direction, horizon, pct, date]


def _page_keyboard(
    *,
    has_more: bool,
    next_offset: object,
    as_of: datetime | None,
) -> InlineKeyboardMarkup | None:
    """Build the "Show 30 more" button, or ``None`` when no more pages.

    Shared by the rich first-page render and the pagination callback so
    the button shape stays identical. Suppressed unless the wire
    reports more rows AND carries an ``as_of`` we can round-trip on the
    next tap.
    """
    if has_more and isinstance(next_offset, int) and as_of is not None:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Show {HISTORY_PAGE_SIZE} more",
                        callback_data=_encode_page_callback(as_of=as_of, offset=next_offset),
                    ),
                ],
            ],
        )
    return None


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
    """True if a ``BadRequest`` is a Rich Message size/limit rejection.

    Plain messages cap at 4096 chars ("message is too long"); Rich
    Messages cap at 32768 chars and 500 blocks, and over-limit edits
    surface as a length / block-count complaint. We match a few
    case-insensitive substrings so a minor wording change on Telegram's
    side doesn't break the fresh-message fallback path.
    """
    text = str(error).lower()
    return "too long" in text or "too many" in text or "limit" in text


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
