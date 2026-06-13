r"""Rich Message rendering + raw-API send helpers (Telegram Bot API 10.1).

Bot API 10.1 (2026-06-11) introduced **Rich Messages** — structured
content with native headings, tables, lists, highlights, block
quotations, and live ``tg://time`` date-time entities — sent via
``sendRichMessage`` and edited via ``editMessageText(rich_message=...)``.

``python-telegram-bot`` 21.x (our pinned range, ``>=21.6,<22``) does not
expose these methods on its typed ``Bot`` API yet, so we drive them
through the bot's raw request layer (``Bot._post``). That layer still
runs ``ExtBot._insert_defaults``, serialises nested objects (an
``InlineKeyboardMarkup`` is JSON-encoded via ``.to_dict()``), and routes
through the ``AIORateLimiter`` — we just bypass the *typed* method that
doesn't exist yet. When a future PTB ships ``Bot.send_rich_message`` the
single ``_post`` call in :func:`send_rich` / :func:`edit_rich` is the
only thing to swap.

This module is the single home for the rich surface so the four read
commands (``/me``, ``/board``, ``/tickers``, ``/history``) don't each
reinvent escaping + table building:

* :func:`escape_inline` / :func:`escape_cell` — neutralise
  user-controlled text. Rich Markdown is GitHub-Flavoured **and may
  contain arbitrary HTML**, so we HTML-escape ``< > &`` in addition to
  backslash-escaping the Markdown inline markers (and, for cells, the
  table-delimiting ``|``). This is a stricter gauntlet than
  :func:`merlin_bot.formatting.escape_md_v2`, which targets MarkdownV2.
* :func:`table` / :func:`table_header` / :func:`table_row` — build GFM
  pipe tables. Callers escape cell content themselves so intended
  inline markup (e.g. a bolded self-row) survives.
* :func:`tg_time` — the ``tg://time`` date-time entity. Renders a
  client-localised timestamp or a live relative countdown that updates
  on the user's device without us re-sending the message.
* :func:`send_rich` / :func:`edit_rich` — the raw-API wrappers.
  :func:`send_rich` falls back to a plain/HTML ``send_message`` if
  Telegram rejects the rich payload (or the bot's API server predates
  10.1) so a reply is never silently dropped during rollout.

Layer note: this lives in the bot layer (it imports ``telegram``) and
does only string building + one Bot API call. No domain/contract import.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import TelegramError

_log = logging.getLogger("merlin.bot.rich")

# Rich message hard limits (Bot API 10.1 "Rich Message Limits"). Exposed
# so paginating surfaces (``/history``) can guard against them before
# Telegram rejects the edit. Blocks include table rows, list items, and
# quotation blocks.
RICH_MESSAGE_MAX_CHARS: int = 32768
RICH_MESSAGE_MAX_BLOCKS: int = 500
RICH_TABLE_MAX_COLUMNS: int = 20

#: Column-alignment markers for a GFM table separator row.
Align = Literal["left", "center", "right"]

_ALIGN_SEPARATOR: dict[Align, str] = {
    "left": ":---",
    "center": ":--:",
    "right": "---:",
}

#: Markdown inline markers backslash-escaped in user text so they render
#: literally rather than toggling bold/italic/code/marked/spoiler/link.
#: ``<``/``>``/``&`` are handled separately (HTML entities) because Rich
#: Markdown allows arbitrary embedded HTML.
_INLINE_MARKERS: tuple[str, ...] = ("*", "_", "~", "`", "=", "[", "]", "!")


def escape_inline(text: str) -> str:
    r"""Escape ``text`` for safe interpolation into rich-markdown prose.

    Used for headings, paragraphs, and link/entity labels — anywhere a
    raw user string is spliced into Rich Markdown outside a table cell.

    Order matters:

    1. Collapse newlines to spaces (a stray ``\n`` would end a heading
       or split a paragraph mid-value).
    2. Escape backslashes first, so the escapes we add next aren't
       themselves doubled.
    3. HTML-escape ``& < >`` — Rich Markdown may contain arbitrary HTML,
       so a literal ``<b>`` in a display name would otherwise open a
       real tag (formatting injection).
    4. Backslash-escape the Markdown inline markers so ``*``/``_``/etc.
       render literally.
    """
    out = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    out = out.replace("\\", "\\\\")
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for marker in _INLINE_MARKERS:
        out = out.replace(marker, f"\\{marker}")
    return out


def escape_cell(text: str) -> str:
    r"""Escape ``text`` for safe interpolation into a table cell.

    Same gauntlet as :func:`escape_inline` plus the table-delimiting
    pipe: an unescaped ``|`` in a cell spawns a phantom column and
    shears the row out of alignment. Telegram only allows *inline*
    formatting in cells, so escaping here is purely defensive against
    user-controlled values (display names, asset symbols).
    """
    return escape_inline(text).replace("|", "\\|")


def table_header(headers: Sequence[str], aligns: Sequence[Align] | None = None) -> str:
    """Render a table's header row + alignment-separator row.

    ``aligns`` defaults to all-left. Length is not validated against
    ``headers`` here — callers pass matching sequences; a mismatch
    surfaces immediately as a visibly broken table in dev.
    """
    cols = list(headers)
    resolved: Sequence[Align] = aligns if aligns is not None else ["left"] * len(cols)
    separators = [_ALIGN_SEPARATOR[a] for a in resolved]
    return f"{_row_line(cols)}\n{_row_line(separators)}"


def table_row(cells: Sequence[str]) -> str:
    """Render a single ``| a | b | c |`` table-body row.

    Exposed separately from :func:`table` so append-only surfaces
    (``/history`` pagination) can splice new rows onto an existing
    table without re-rendering the header.
    """
    return _row_line(cells)


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[Align] | None = None,
) -> str:
    """Render a full GFM pipe table (header + separator + body rows).

    Cells are emitted verbatim — callers are responsible for escaping
    user-controlled content via :func:`escape_cell` while leaving
    intended inline markup (e.g. ``**self**``) intact.
    """
    lines = [table_header(headers, aligns)]
    lines.extend(_row_line(row) for row in rows)
    return "\n".join(lines)


def _row_line(cells: Sequence[str]) -> str:
    """Join cells into a single pipe-delimited table line."""
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def tg_time(when: datetime, *, fmt: str = "r", label: str = "") -> str:
    """Build a ``tg://time`` date-time entity for ``when``.

    Renders as a client-localised timestamp the recipient sees in their
    own timezone/locale — and, with ``fmt="r"``, a **live relative
    countdown** ("in 3 hours") that updates on the device without us
    re-sending. ``label`` is the fallback text shown if a client can't
    render the entity; pass a human string (e.g. the absolute UTC time)
    so the degraded form is still readable. The label is escaped because
    it sits inside the ``![...]`` span.

    ``fmt`` follows Telegram's date-time format grammar ``r|w?[dD]?[tT]?``:
    ``r`` relative, ``w`` weekday, ``d`` short date, ``D`` long date,
    ``t`` short time, ``T`` long time.

    A naive ``when`` is assumed to be UTC — ``datetime.timestamp()`` would
    otherwise interpret it in the host's local timezone and emit the
    wrong epoch.
    """
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    unix = int(aware.timestamp())
    safe_label = escape_inline(label) if label else ""
    return f"![{safe_label}](tg://time?unix={unix}&format={fmt})"


async def send_rich(
    bot: Bot,
    chat_id: int,
    markdown: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    fallback_text: str,
    fallback_parse_mode: str | None = None,
) -> None:
    """Send a Rich Message, falling back to a plain/HTML send on failure.

    Drives ``sendRichMessage`` through the raw request layer (see module
    docstring for why). If Telegram rejects the rich payload — an older
    bot-API server, a malformed block, or a transient error — we log and
    re-send ``fallback_text`` via the typed ``send_message`` so the user
    always gets a reply. ``fallback_parse_mode`` is the parse mode for
    that fallback (e.g. ``ParseMode.HTML`` for ``/board``); the rich send
    itself never uses ``parse_mode``.
    """
    data = _rich_payload(chat_id, markdown, reply_markup=reply_markup)
    try:
        await bot._post("sendRichMessage", data)
        return
    except TelegramError as exc:
        _log.warning("sendRichMessage failed (%s); falling back to plain send", exc)

    await bot.send_message(
        chat_id=chat_id,
        text=fallback_text,
        parse_mode=fallback_parse_mode,
        reply_markup=reply_markup,
    )


async def edit_rich(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    markdown: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit an existing message into a Rich Message in place.

    Drives ``editMessageText(rich_message=...)`` through the raw request
    layer. Unlike :func:`send_rich` this does **not** swallow errors:
    callers (``/board`` filter switch, ``/history`` pagination) have
    their own ``BadRequest`` handling — "message is not modified" and
    "message too long" both need bespoke recovery — so we let the
    ``TelegramError`` propagate.
    """
    data = _rich_payload(chat_id, markdown, reply_markup=reply_markup)
    data["message_id"] = message_id
    await bot._post("editMessageText", data)


#: Custom-emoji id of the animated "thinking" indicator from Telegram's
#: AIActions pack (https://t.me/addemoji/AIActions) — the emoji Telegram
#: recommends for the thinking placeholder. Looked up once via
#: ``getStickerSet(name="AIActions")``; static because the pack is fixed.
_THINKING_EMOJI_ID: str = "5573473356579078196"

#: HTML streamed into the "Thinking" draft bubble: the animated custom
#: emoji (💭 is the fallback glyph for clients/recipients that can't
#: render custom emoji) followed by the label.
#:
#: We stream *non-empty* text via the plain ``sendMessageDraft`` because,
#: from live testing: an *empty* draft renders as a literal "[]" on the
#: client (not the documented placeholder), and the rich ``<tg-thinking>``
#: (``RichBlockThinking``) block drops plain inner text. Telegram appends
#: its own animated "…" on the client, so we don't.
_THINKING_DRAFT_HTML: str = f'<tg-emoji emoji-id="{_THINKING_EMOJI_ID}">💭</tg-emoji> Thinking'


async def send_thinking_draft(bot: Bot, chat_id: int, *, draft_id: int) -> None:
    """Show a transient "Thinking" bubble (Bot API 10.1 message draft).

    Streams a ``sendMessageDraft`` whose HTML body is the recommended
    animated AIActions custom emoji + "Thinking" label — Telegram renders
    it as a partial/streamed draft bubble while the bot works on a slow
    step (e.g. the ``/alpha`` freeform LLM extraction). The draft is
    ephemeral (~30s) and is superseded by the next real message the bot
    sends to the chat, so callers just fire this before the slow step and
    then send their actual reply as usual — no explicit dismissal needed.
    (See ``_THINKING_DRAFT_HTML`` for why we stream HTML text rather than
    an empty draft or a rich ``<tg-thinking>`` block.)

    Best-effort and non-fatal: message drafts are a brand-new Bot API
    10.1 feature with no typed PTB support, render only on updated
    clients, and are purely cosmetic — so any Telegram-side failure
    (older bot-API server, non-private chat, etc.) is swallowed at debug
    level. The caller's real reply is what matters.

    ``draft_id`` must be non-zero (Telegram rejects 0); reusing the same
    id animates an update to a live draft. Callers typically pass the
    triggering message id.
    """
    try:
        await bot._post(
            "sendMessageDraft",
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": _THINKING_DRAFT_HTML,
                "parse_mode": "HTML",
            },
        )
    except TelegramError as exc:
        _log.debug("sendMessageDraft (thinking bubble) failed: %s", exc)


def _rich_payload(
    chat_id: int,
    markdown: str,
    *,
    reply_markup: InlineKeyboardMarkup | None,
) -> dict[str, Any]:
    """Assemble the ``sendRichMessage`` / ``editMessageText`` request body.

    ``rich_message`` is an ``InputRichMessage`` with the ``markdown``
    field set. ``reply_markup`` is left as the PTB object — the request
    layer serialises it via ``.to_dict()`` (see ``RequestParameter``).
    """
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"markdown": markdown},
    }
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    return data
