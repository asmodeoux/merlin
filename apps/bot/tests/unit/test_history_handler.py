"""Unit tests for the ``/history`` handler.

Four things to pin:

1. Rendering: emoji dispatch matches the live resolution
   notifications (🎉 / 🙂‍↔️ / 📝), one row per resolved call, dates
   render as "Mon DD" calendar form.
2. Pagination: the inline "Show 30 more" button appears only when
   ``has_more=True``; the callback data carries the next offset
   verbatim and the pattern accepts non-negative integers only.
3. Empty-state copy: cold-start source reads "No resolved calls
   yet" — never a blank list.
4. Error paths: 404 → /start hint; 5xx + transport → recoverable
   copy. The Show-30-more click failure shows a toast alert without
   destroying the existing message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.history import (
    HISTORY_PAGE_SIZE,
    _decode_page_callback,
    _encode_page_callback,
    _format_pct,
    _format_short_date,
    _pick_emoji,
    _render_history,
    _render_history_rich,
    build_callback_handler,
    build_handler,
    history_command,
    on_page_callback,
)
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

USER_ID = 42
CHAT_ID = 99
MESSAGE_ID = 7


@dataclass
class _FakeUser:
    id: int


@dataclass
class _FakeChat:
    id: int


@dataclass
class _FakeMessage:
    text: str | None = None
    chat_id: int = CHAT_ID
    message_id: int = MESSAGE_ID
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    edits: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append((text, kw))


@dataclass
class _FakeQuery:
    """Stand-in for ``telegram.CallbackQuery``.

    ``edit_message_text_impl`` lets a test inject a custom handler
    (e.g. one that raises ``BadRequest("message is too long")``) so
    the fallback-path tests can exercise the message-too-long branch
    without monkey-patching PTB.
    """

    data: str
    message: _FakeMessage | None
    answers: list[tuple[str | None, dict[str, Any]]] = field(default_factory=list)
    edits: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    markup_edits: list[Any] = field(default_factory=list)
    edit_message_text_impl: Any = None

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append((text, kw))

    async def edit_message_text(self, text: str, **kw: Any) -> None:
        self.edits.append((text, kw))
        if self.edit_message_text_impl is not None:
            await self.edit_message_text_impl(text, **kw)

    async def edit_message_reply_markup(self, *, reply_markup: Any = None) -> None:
        self.markup_edits.append(reply_markup)


@dataclass
class _FakeBot:
    """Captures raw Bot API calls for the rich send/edit paths.

    ``posts`` records ``(endpoint, data)`` for ``sendRichMessage`` /
    ``editMessageText``; ``post_side_effects`` maps an endpoint to a
    one-shot exception (so a too-long edit can be simulated while a
    follow-up fresh send still succeeds). ``sent_messages`` is the
    plain-text ``send_rich`` fallback sink.
    """

    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent_messages: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    post_side_effects: dict[str, Exception] = field(default_factory=dict)

    async def _post(self, endpoint: str, data: dict[str, Any] | None = None, **_kw: Any) -> Any:
        self.posts.append((endpoint, data or {}))
        exc = self.post_side_effects.pop(endpoint, None)
        if exc is not None:
            raise exc

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent_messages.append((chat_id, text, kw))


def _posted(bot: _FakeBot, endpoint: str) -> dict[str, Any]:
    """Return the data of the last ``endpoint`` post."""
    for posted_endpoint, data in reversed(bot.posts):
        if posted_endpoint == endpoint:
            return data
    raise AssertionError(f"no {endpoint} post captured; got {[e for e, _ in bot.posts]}")


@dataclass
class _FakeUpdate:
    effective_user: _FakeUser | None = None
    message: _FakeMessage | None = None
    callback_query: _FakeQuery | None = None
    effective_chat: _FakeChat | None = None


@dataclass
class _FakeContext:
    bot_data: dict[str, Any]
    bot: _FakeBot = field(default_factory=_FakeBot)


def _entry(
    *,
    asset_symbol: str = "ETH",
    direction: str = "long",
    horizon_seconds: int = 86_400,
    observed_return_pct: float = 2.0,
    is_correct: bool = True,
    is_flat: bool = False,
    resolved_at: str | None = None,
) -> dict[str, Any]:
    """Builder for a wire ``HistoryEntry`` dict."""
    return {
        "claim_id": str(uuid4()),
        "asset_symbol": asset_symbol,
        "direction": direction,
        "horizon_seconds": horizon_seconds,
        "observed_return_pct": observed_return_pct,
        "is_correct": is_correct,
        "is_flat": is_flat,
        "resolved_at": resolved_at or "2026-04-28T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Emoji dispatch — must mirror the resolution notifications.
# ---------------------------------------------------------------------------


def test_pick_emoji_won_is_party_popper() -> None:
    assert _pick_emoji(is_correct=True, is_flat=False) == "🎉"


def test_pick_emoji_lost_is_face_with_diagonal_eyes() -> None:
    assert _pick_emoji(is_correct=False, is_flat=False) == "🙂‍↔️"


def test_pick_emoji_flat_is_memo() -> None:
    assert _pick_emoji(is_correct=False, is_flat=True) == "📝"


def test_pick_emoji_won_wins_on_inconsistent_inputs() -> None:
    """``is_correct=True, is_flat=True`` is impossible by construction
    (``is_correct_call`` returns False on flats) — but on a future
    contract drift, the renderer favours the positive icon (won)
    rather than demoting a real win to a flat."""
    assert _pick_emoji(is_correct=True, is_flat=True) == "🎉"


# ---------------------------------------------------------------------------
# Date / pct rendering
# ---------------------------------------------------------------------------


def test_format_short_date_renders_month_day_no_year_when_current_year() -> None:
    """Current-year rows render as ``Apr 28`` (no year). The live ping
    gave the user the exact moment; the list shows the rough date."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _format_short_date("2026-04-28T12:00:00Z", now=now) == "Apr 28"


def test_format_short_date_strips_leading_zero_on_day() -> None:
    """``Apr 5`` not ``Apr 05`` — single-digit days read cleaner."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _format_short_date("2026-04-05T12:00:00Z", now=now) == "Apr 5"


def test_format_short_date_appends_year_for_out_of_year_rows() -> None:
    """A row from a previous year must carry the year so users with
    multi-year history can tell ``Apr 28`` (this year) apart from
    ``Apr 28, 2025`` (last year)."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _format_short_date("2025-04-28T12:00:00Z", now=now) == "Apr 28, 2025"
    assert _format_short_date("2024-12-31T23:59:00Z", now=now) == "Dec 31, 2024"


def test_format_short_date_falls_back_on_garbage() -> None:
    """Malformed timestamp → em-dash. Never crash the message."""
    assert _format_short_date("") == "—"
    assert _format_short_date("not-a-date") == "—"
    assert _format_short_date(None) == "—"


def test_format_pct_one_decimal_signed() -> None:
    assert _format_pct(7.6) == "+7.6%"
    assert _format_pct(-7.6) == "-7.6%"
    assert _format_pct(0.0) == "+0.0%"


def test_format_pct_falls_back_on_non_numeric() -> None:
    assert _format_pct(None) == "—%"
    assert _format_pct("nope") == "—%"


# ---------------------------------------------------------------------------
# Row rendering + pagination
# ---------------------------------------------------------------------------


_PIN = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
_PIN_ISO = "2026-04-28T12:00:00Z"
_PIN_EPOCH = int(_PIN.timestamp())


def test_render_history_first_page_with_more_appends_button() -> None:
    """``has_more=True`` → inline keyboard with one button carrying
    the next offset + the frozen ``as_of`` on its callback_data.

    The button rendering is gated on a non-None ``as_of`` — without
    it the bot can't round-trip the frozen window, so it suppresses
    the button rather than rendering one that would drift the page
    boundaries on the next click.
    """
    payload = {
        "entries": [
            _entry(
                asset_symbol="TON",
                direction="short",
                horizon_seconds=86_400,
                observed_return_pct=-7.6,
                is_correct=True,
                resolved_at="2026-04-28T12:00:00Z",
            ),
        ],
        "has_more": True,
        "next_offset": 30,
        "as_of": _PIN_ISO,
    }

    text, keyboard = _render_history(payload, prior_text=None)

    assert text.startswith("Your resolved calls")
    # Date renders without year — current-year test scaffolding will
    # likely pass since the fixture year matches "now" for most runs,
    # but the row text is asserted only on the ticker / pct / shape.
    assert "🎉 TON — Short over 24 hours ended -7.6%" in text
    assert isinstance(keyboard, InlineKeyboardMarkup)
    rows = keyboard.inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    button = rows[0][0]
    assert button.text == "Show 30 more"
    # Callback shape: ``history:page:<as_of_epoch>:<offset>``.
    assert button.callback_data == f"history:page:{_PIN_EPOCH}:30"
    # Footer always renders.
    assert "/alpha" in text
    assert "/tickers" in text


def test_render_history_suppresses_button_when_as_of_missing() -> None:
    """A wire payload without ``as_of`` cannot be paginated safely —
    without the frozen window the next click would drift. The bot
    suppresses the button rather than render a broken one."""
    payload = {
        "entries": [_entry(asset_symbol="TON")],
        "has_more": True,
        "next_offset": 30,
        "as_of": None,
    }
    _text, keyboard = _render_history(payload, prior_text=None)
    assert keyboard is None


def test_render_history_last_page_drops_button() -> None:
    """``has_more=False`` → no inline keyboard at all. The button is
    gone, the rows + footer stay."""
    payload = {
        "entries": [_entry(asset_symbol="TON")],
        "has_more": False,
        "next_offset": None,
        "as_of": _PIN_ISO,
    }
    text, keyboard = _render_history(payload, prior_text=None)
    assert keyboard is None
    assert "Show 30 more" not in text


def test_render_history_empty_state_collapses_to_cold_start_copy() -> None:
    """Cold-start source: zero entries → the empty-state copy. No
    keyboard, no bullet rows, no zero placeholder."""
    payload = {
        "entries": [],
        "has_more": False,
        "next_offset": None,
        "as_of": _PIN_ISO,
    }
    text, keyboard = _render_history(payload, prior_text=None)
    assert "No resolved calls yet" in text
    assert keyboard is None


def test_render_history_follow_up_page_appends_below_existing_rows() -> None:
    """A "Show 30 more" click extends the existing message: the
    previous body stays, the new rows append below, the footer
    re-renders at the bottom."""
    first_page = {
        "entries": [_entry(asset_symbol="TON", resolved_at="2026-04-28T12:00:00Z")],
        "has_more": True,
        "next_offset": 1,
        "as_of": _PIN_ISO,
    }
    first_text, _kb = _render_history(first_page, prior_text=None)
    assert "TON" in first_text
    assert "Show 30 more" not in first_text  # Button is on keyboard, not in text.

    second_page = {
        "entries": [_entry(asset_symbol="BTC", resolved_at="2026-04-27T12:00:00Z")],
        "has_more": False,
        "next_offset": None,
        "as_of": _PIN_ISO,
    }
    new_text, new_kb = _render_history(second_page, prior_text=first_text)

    # Both rows present after the follow-up.
    assert "TON" in new_text
    assert "BTC" in new_text
    # TON renders before BTC (preserved order from prior + new appended below).
    assert new_text.index("TON") < new_text.index("BTC")
    # Footer renders exactly once at the bottom.
    assert new_text.count("/tickers for your per-ticker view") == 1
    # Last page → button is gone.
    assert new_kb is None


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def test_render_history_rich_builds_table_with_emoji_and_button() -> None:
    """The rich render is a heading + a six-column table (outcome emoji
    first) + footer, with the "Show 30 more" button when ``has_more``."""
    payload = {
        "entries": [
            _entry(
                asset_symbol="TON",
                direction="short",
                horizon_seconds=86_400,
                observed_return_pct=-7.6,
                is_correct=False,
                is_flat=False,
                resolved_at=_PIN_ISO,
            ),
        ],
        "has_more": True,
        "next_offset": 30,
        "as_of": _PIN_ISO,
    }
    text, keyboard = _render_history_rich(payload)
    assert text.startswith("# Your resolved calls")
    assert "|  | Asset | Call | Horizon | Return | Resolved |" in text
    assert "| 🙂‍↔️ | TON | Short | 24 hours | -7.6% | Apr 28 |" in text
    assert "/tickers for your per-ticker view" in text
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert keyboard.inline_keyboard[0][0].callback_data == f"history:page:{_PIN_EPOCH}:30"


def test_render_history_rich_empty_state_has_no_table() -> None:
    """Cold-start → plain empty-state copy, no table, no button."""
    text, keyboard = _render_history_rich({"entries": []})
    assert "No resolved calls yet" in text
    assert "|" not in text
    assert keyboard is None


def test_render_history_rich_escapes_pipe_in_symbol() -> None:
    """A wire-drift asset symbol with a pipe is escaped so it can't
    shear the table."""
    text, _kb = _render_history_rich(
        {
            "entries": [_entry(asset_symbol="a|b")],
            "has_more": False,
            "next_offset": None,
            "as_of": _PIN_ISO,
        },
    )
    assert r"A\|B" in text


def test_encode_decode_page_callback_roundtrips() -> None:
    """The callback-data shape must roundtrip cleanly so a stale
    button click never gets a forged offset or ``as_of``."""
    encoded = _encode_page_callback(as_of=_PIN, offset=60)
    assert encoded == f"history:page:{_PIN_EPOCH}:60"
    as_of, offset = _decode_page_callback(encoded)
    assert as_of == _PIN
    assert offset == 60


def test_encode_page_callback_rejects_negative_offset() -> None:
    """A negative offset would violate the API's ``ge=0`` bound on the
    history limit/offset Query — reject at the encoding boundary."""
    with pytest.raises(ValueError):
        _encode_page_callback(as_of=_PIN, offset=-1)


def test_encode_page_callback_rejects_pre_epoch_as_of() -> None:
    """A negative epoch ``as_of`` would round-trip into the callback
    data as a negative integer and fall through the pattern's
    ``\\d+`` filter — reject at the encoding boundary."""
    pre_epoch = datetime(1969, 12, 31, tzinfo=UTC)
    with pytest.raises(ValueError):
        _encode_page_callback(as_of=pre_epoch, offset=0)


def test_decode_page_callback_rejects_malformed_prefix() -> None:
    """Defensive: a payload from a different callback handler must
    NOT decode as a history page offset."""
    with pytest.raises(ValueError):
        _decode_page_callback("alpha:confirm")


def test_history_page_callback_pattern_accepts_only_two_non_negative_integers() -> None:
    """The CallbackQueryHandler's pattern is the first line of defence
    — only ``history:page:<as_of_epoch>:<offset>`` (both non-negative
    integers) should match. Anything else MUST NOT reach the handler."""
    handler = build_callback_handler()
    pattern = handler.pattern
    assert pattern is not None
    pattern_str = pattern.pattern if hasattr(pattern, "pattern") else str(pattern)
    regex = re.compile(pattern_str)
    assert regex.match(f"history:page:{_PIN_EPOCH}:0") is not None
    assert regex.match(f"history:page:{_PIN_EPOCH}:30") is not None
    assert regex.match("history:page:0:0") is not None
    # Negative either component → rejected.
    assert regex.match(f"history:page:{_PIN_EPOCH}:-1") is None
    assert regex.match("history:page:-1:0") is None
    # Non-integer suffix → rejected.
    assert regex.match(f"history:page:{_PIN_EPOCH}:abc") is None
    # Missing offset → rejected.
    assert regex.match(f"history:page:{_PIN_EPOCH}:") is None
    assert regex.match(f"history:page:{_PIN_EPOCH}") is None
    # Old single-integer shape (pre-as_of) must NOT match anymore.
    assert regex.match("history:page:30") is None
    # Unrelated prefix → rejected.
    assert regex.match("alpha:confirm") is None


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_history_command_fetches_first_page_and_renders() -> None:
    """Happy path: /history hits the API with the caller's tg id +
    default ``limit/offset``, then sends the first page as a Rich
    Message."""
    client = AsyncMock()
    client.get_my_history.return_value = {
        "entries": [_entry(asset_symbol="TON")],
        "has_more": False,
        "next_offset": None,
    }
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await history_command(update, ctx)  # type: ignore[arg-type]

    client.get_my_history.assert_awaited_once_with(
        tg_user_id=USER_ID,
        limit=HISTORY_PAGE_SIZE,
        offset=0,
    )
    assert update.message is not None
    assert update.message.replies == []
    data = _posted(bot, "sendRichMessage")
    assert data["chat_id"] == CHAT_ID
    markdown = data["rich_message"]["markdown"]
    assert markdown.startswith("# Your resolved calls")
    assert "TON" in markdown
    # Last page → no keyboard on the send.
    assert data.get("reply_markup") is None


async def test_history_command_attaches_keyboard_when_has_more() -> None:
    """``has_more=True`` → the rich send carries an InlineKeyboardMarkup
    with the "Show 30 more" button + correct next-offset encoded in
    callback_data, alongside the frozen ``as_of`` epoch."""
    client = AsyncMock()
    client.get_my_history.return_value = {
        "entries": [_entry(asset_symbol="TON")],
        "has_more": True,
        "next_offset": 30,
        "as_of": _PIN_ISO,
    }
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await history_command(update, ctx)  # type: ignore[arg-type]

    data = _posted(bot, "sendRichMessage")
    keyboard = data.get("reply_markup")
    assert isinstance(keyboard, InlineKeyboardMarkup)
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == f"history:page:{_PIN_EPOCH}:30"


async def test_history_command_404_points_at_start() -> None:
    """User typed /history but never finished /start."""
    client = AsyncMock()
    client.get_my_history.side_effect = MerlinHTTPError(404, "source not found")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await history_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "/start" in body


async def test_history_command_recoverable_error_reply() -> None:
    """5xx → recoverable copy, no raw status codes leak."""
    client = AsyncMock()
    client.get_my_history.side_effect = MerlinHTTPError(503, "down")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await history_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "moment" in body.lower() or "try" in body.lower()
    assert "503" not in body


async def test_history_command_handles_missing_message_silently() -> None:
    """No message → no-op."""
    client = AsyncMock()
    update = _FakeUpdate(effective_user=_FakeUser(id=USER_ID), message=None)
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await history_command(update, ctx)  # type: ignore[arg-type]

    client.get_my_history.assert_not_called()


# ---------------------------------------------------------------------------
# Callback handler — "Show 30 more" advance.
# ---------------------------------------------------------------------------


async def test_on_page_callback_fetches_window_and_edits_message() -> None:
    """A "Show 30 more" click must:

    1. Re-fetch the accumulated window ``[0, offset + page)`` from the
       frozen ``as_of`` (rich messages can't be rebuilt from the prior
       body — so the whole table is re-rendered each tap).
    2. Edit the original message in place via ``editMessageText`` with
       the full table.
    3. ACK exactly once — AFTER the successful edit — so the loading
       spinner stops and Telegram does not reject a second answer for
       the same callback id.
    """
    client = AsyncMock()
    # The re-fetched window carries the full accumulated list.
    client.get_my_history.return_value = {
        "entries": [
            _entry(asset_symbol="TON", resolved_at="2026-04-28T12:00:00Z"),
            _entry(asset_symbol="BTC", resolved_at="2026-04-27T12:00:00Z"),
        ],
        "has_more": False,
        "next_offset": None,
        "as_of": _PIN_ISO,
    }
    query = _FakeQuery(data=f"history:page:{_PIN_EPOCH}:1", message=_FakeMessage())
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        callback_query=query,
        effective_chat=_FakeChat(id=CHAT_ID),
    )
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    # offset=1 rows shown → target window of 31 rows fetched from 0.
    client.get_my_history.assert_awaited_once_with(
        tg_user_id=USER_ID,
        limit=1 + HISTORY_PAGE_SIZE,
        offset=0,
        as_of=_PIN_ISO,
    )
    # Exactly one ACK — Telegram rejects a second ``answer()`` for the
    # same callback id, so the success path ACKs once at the end.
    assert len(query.answers) == 1
    assert query.answers[0] == (None, {})
    # Exactly one in-place edit via editMessageText.
    data = _posted(bot, "editMessageText")
    assert data["chat_id"] == CHAT_ID
    assert data["message_id"] == MESSAGE_ID
    new_text = data["rich_message"]["markdown"]
    # Both rows present, in order.
    assert "TON" in new_text
    assert "BTC" in new_text
    assert new_text.index("TON") < new_text.index("BTC")
    # No more pages → keyboard removed.
    assert data.get("reply_markup") is None


async def test_on_page_callback_failure_acks_once_with_alert() -> None:
    """A 5xx during page advance must surface as a single toast alert
    (one ``answer()`` call — Telegram rejects a second). Existing
    message stays put so the user can retry without losing context.
    """
    client = AsyncMock()
    client.get_my_history.side_effect = MerlinHTTPError(503, "down")
    message = _FakeMessage(text="Your resolved calls")
    query = _FakeQuery(data=f"history:page:{_PIN_EPOCH}:30", message=message)
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        callback_query=query,
        effective_chat=_FakeChat(id=99),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    # Exactly one ``answer`` call — the show_alert toast. A second
    # would 400 against the Telegram API (query is too old / id
    # invalid) so the bot must NOT ACK + alert separately.
    assert len(query.answers) == 1
    alert_text, alert_kw = query.answers[0]
    assert alert_kw.get("show_alert") is True
    assert alert_text is not None
    # No message edit on failure — leave the rows the user was reading.
    assert query.edits == []


async def test_on_page_callback_malformed_data_acks_with_alert() -> None:
    """If the pattern matcher somehow lets a malformed payload through
    (it shouldn't), the handler ACKs with an error toast rather than
    crashing or silently dying with the spinner stuck. Defence in
    depth — the pattern is the first line."""
    client = AsyncMock()
    message = _FakeMessage(text="Your resolved calls")
    # Pattern-bypassing payload (missing offset).
    query = _FakeQuery(data=f"history:page:{_PIN_EPOCH}", message=message)
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        callback_query=query,
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    client.get_my_history.assert_not_called()
    assert len(query.answers) == 1
    assert query.answers[0][1].get("show_alert") is True


async def test_on_page_callback_rich_too_large_falls_back_to_fresh_reply() -> None:
    """If the accumulated table exceeds a Rich Message limit,
    ``editMessageText`` raises ``BadRequest`` (e.g. "message is too
    long"). The handler must catch it, drop the prior keyboard, and
    send the window as a fresh message so the user keeps paginating
    instead of staring at a silent button."""
    client = AsyncMock()
    client.get_my_history.return_value = {
        "entries": [
            _entry(asset_symbol="TON", resolved_at="2026-04-28T12:00:00Z"),
            _entry(asset_symbol="BTC", resolved_at="2026-04-27T12:00:00Z"),
        ],
        "has_more": True,
        "next_offset": 60,
        "as_of": _PIN_ISO,
    }
    query = _FakeQuery(data=f"history:page:{_PIN_EPOCH}:30", message=_FakeMessage())
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        callback_query=query,
        effective_chat=_FakeChat(id=CHAT_ID),
    )
    bot = _FakeBot(post_side_effects={"editMessageText": BadRequest("Message is too long")})
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    # offset=30 → window of 60 rows fetched from 0 (fits the cap).
    client.get_my_history.assert_awaited_once_with(
        tg_user_id=USER_ID,
        limit=30 + HISTORY_PAGE_SIZE,
        offset=0,
        as_of=_PIN_ISO,
    )
    # Single ACK after fallback decision.
    assert len(query.answers) == 1
    # Edit was attempted (and raised); the prior keyboard was then
    # stripped via ``edit_message_reply_markup``, and a fresh rich
    # send carried the window.
    assert [endpoint for endpoint, _ in bot.posts] == ["editMessageText", "sendRichMessage"]
    assert query.markup_edits == [None]
    fresh = _posted(bot, "sendRichMessage")
    assert fresh["chat_id"] == CHAT_ID
    assert "BTC" in fresh["rich_message"]["markdown"]
    # The fresh reply's button encodes the next offset (still has more).
    fresh_kb = fresh.get("reply_markup")
    assert isinstance(fresh_kb, InlineKeyboardMarkup)
    assert fresh_kb.inline_keyboard[0][0].callback_data == f"history:page:{_PIN_EPOCH}:60"


async def test_on_page_callback_inaccessible_message_falls_back_to_fresh_reply() -> None:
    """If ``query.message`` is ``None`` (the original message was
    deleted), the handler can't edit. It ACKs and sends a fresh
    reply so the user keeps reading instead of seeing the spinner
    stop with no visible effect."""
    client = AsyncMock()
    client.get_my_history.return_value = {
        "entries": [_entry(asset_symbol="BTC", resolved_at="2026-04-27T12:00:00Z")],
        "has_more": False,
        "next_offset": None,
        "as_of": _PIN_ISO,
    }
    query = _FakeQuery(data=f"history:page:{_PIN_EPOCH}:30", message=None)
    chat = _FakeChat(id=77)
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        callback_query=query,
        effective_chat=chat,
    )
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    # ACKed once, no edit (no message to edit), one fresh rich send.
    assert len(query.answers) == 1
    assert [endpoint for endpoint, _ in bot.posts] == ["sendRichMessage"]
    fresh = _posted(bot, "sendRichMessage")
    assert fresh["chat_id"] == 77
    assert "BTC" in fresh["rich_message"]["markdown"]


async def test_on_page_callback_short_circuits_on_missing_query() -> None:
    """Defensive: no callback_query on the update → no-op. PTB
    routes this update via the pattern so the branch should be
    unreachable, but the handler must not crash if it ever fires."""
    client = AsyncMock()
    update = _FakeUpdate(effective_user=_FakeUser(id=USER_ID))
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await on_page_callback(update, ctx)  # type: ignore[arg-type]

    client.get_my_history.assert_not_called()


# ---------------------------------------------------------------------------
# Build_handler
# ---------------------------------------------------------------------------


def test_build_handler_returns_command_handler_for_history() -> None:
    handler = build_handler()
    assert isinstance(handler, CommandHandler)
    assert "history" in handler.commands


def test_build_callback_handler_returns_callbackqueryhandler() -> None:
    handler = build_callback_handler()
    assert isinstance(handler, CallbackQueryHandler)


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")
def test_history_handler_and_callback_registered_in_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot's ``build_application`` must include the ``/history``
    command handler AND its ``history:page:`` callback handler —
    otherwise the slash menu lies and the "Show 30 more" button
    falls through to silence."""
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
    callback_patterns: list[str] = []
    for group in application.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                commands_registered.update(handler.commands)
            elif isinstance(handler, CallbackQueryHandler):
                pattern = handler.pattern
                if pattern is not None:
                    pattern_str = pattern.pattern if hasattr(pattern, "pattern") else str(pattern)
                    callback_patterns.append(pattern_str)

    assert "history" in commands_registered
    assert any("history:page:" in p for p in callback_patterns), (
        "history:page:<offset> callback handler must be wired into the "
        "application — without it the 'Show 30 more' button stays silent"
    )


def _utc_now_iso() -> str:
    """Helper for asserting we don't accidentally rely on imports."""
    return datetime.now(tz=UTC).isoformat()
