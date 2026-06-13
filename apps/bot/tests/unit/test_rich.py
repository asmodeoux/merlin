"""Unit tests for ``merlin_bot.rich`` — the Rich Message (Bot API 10.1)
rendering + raw-API send layer.

Pins four things:

1. Escaping: user-controlled text is neutralised for Rich Markdown,
   which is GFM **and may contain arbitrary HTML** — so ``< > &`` are
   HTML-escaped on top of backslash-escaping the inline markers, and
   table cells additionally escape the delimiting ``|``.
2. Table building: GFM pipe tables with per-column alignment markers.
3. ``tg_time``: the ``tg://time`` date-time entity (live countdowns).
4. ``send_rich`` / ``edit_rich``: drive the raw ``sendRichMessage`` /
   ``editMessageText`` endpoints; ``send_rich`` falls back to a plain
   ``send_message`` when Telegram rejects the rich payload, while
   ``edit_rich`` lets the error propagate for callers to handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from merlin_bot import rich
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError


@dataclass
class _FakeBot:
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    post_side_effect: Exception | None = None

    async def _post(self, endpoint: str, data: dict[str, Any] | None = None, **_kw: Any) -> Any:
        self.posts.append((endpoint, data or {}))
        if self.post_side_effect is not None:
            raise self.post_side_effect

    async def send_message(self, **kw: Any) -> None:
        self.sent.append(kw)


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_escape_inline_collapses_newlines() -> None:
    """Newlines would end a heading / split a paragraph mid-value."""
    assert rich.escape_inline("a\nb\r\nc\rd") == "a b c d"


def test_escape_inline_html_escapes_angle_brackets_and_amp() -> None:
    """Rich Markdown allows embedded HTML, so a raw ``<b>`` in user text
    would open a real tag — neutralise via entities."""
    assert rich.escape_inline("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"


def test_escape_inline_backslash_escapes_markdown_markers() -> None:
    """``* _ ~ ` = [ ] !`` render literally after escaping."""
    assert rich.escape_inline("*b* _i_ ~s~ `c` ==m== [l] !") == (
        r"\*b\* \_i\_ \~s\~ \`c\` \=\=m\=\= \[l\] \!"
    )


def test_escape_inline_doubles_backslash_first() -> None:
    """A literal backslash is doubled before we add our own escapes, so
    the two don't get conflated."""
    assert rich.escape_inline("a\\b") == "a\\\\b"


def test_escape_cell_also_escapes_pipe() -> None:
    """A pipe in a cell would spawn a phantom column."""
    assert rich.escape_cell("a|b") == "a\\|b"
    # Inherits the inline gauntlet too.
    assert rich.escape_cell("<x>|*y*") == "&lt;x&gt;\\|\\*y\\*"


def test_escape_inline_leaves_pipe_untouched() -> None:
    """Outside a table the pipe is not special, so ``escape_inline``
    leaves it (only ``escape_cell`` escapes it)."""
    assert "|" in rich.escape_inline("a|b")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_table_header_renders_alignment_markers() -> None:
    header = rich.table_header(["A", "B", "C"], ["left", "center", "right"])
    assert header == "| A | B | C |\n| :--- | :--: | ---: |"


def test_table_header_defaults_to_left_alignment() -> None:
    header = rich.table_header(["A", "B"])
    assert header == "| A | B |\n| :--- | :--- |"


def test_table_row_joins_cells() -> None:
    assert rich.table_row(["1", "two", "3"]) == "| 1 | two | 3 |"


def test_table_assembles_header_separator_and_rows() -> None:
    out = rich.table(
        ["Ticker", "PnL"],
        [["BTC", "+1.0%"], ["ETH", "-2.0%"]],
        ["left", "right"],
    )
    assert out == ("| Ticker | PnL |\n| :--- | ---: |\n| BTC | +1.0% |\n| ETH | -2.0% |")


# ---------------------------------------------------------------------------
# tg_time
# ---------------------------------------------------------------------------


def test_tg_time_builds_entity_with_unix_and_format() -> None:
    when = datetime(2026, 6, 12, 16, 35, tzinfo=UTC)
    unix = int(when.timestamp())
    assert rich.tg_time(when, fmt="r") == f"![](tg://time?unix={unix}&format=r)"


def test_tg_time_defaults_to_relative_format() -> None:
    when = datetime(2026, 6, 12, 16, 35, tzinfo=UTC)
    assert "&format=r)" in rich.tg_time(when)


def test_tg_time_normalizes_naive_datetime_as_utc() -> None:
    """A naive datetime is assumed UTC — otherwise ``.timestamp()`` would
    use the host's local timezone and emit the wrong epoch (the bubble
    would point at the wrong instant on the user's device)."""
    aware = datetime(2026, 6, 12, 16, 35, tzinfo=UTC)
    naive = datetime(2026, 6, 12, 16, 35)
    aware_unix = int(aware.timestamp())
    assert rich.tg_time(naive) == f"![](tg://time?unix={aware_unix}&format=r)"


def test_tg_time_escapes_label() -> None:
    """The fallback label sits inside the ``![...]`` span and is escaped
    so a metacharacter can't break the entity."""
    when = datetime(2026, 6, 12, 16, 35, tzinfo=UTC)
    out = rich.tg_time(when, fmt="D", label="June 12 [soon]")
    assert out.startswith(r"![June 12 \[soon\]](tg://time?unix=")
    assert "&format=D)" in out


# ---------------------------------------------------------------------------
# send_rich / edit_rich
# ---------------------------------------------------------------------------


async def test_send_rich_posts_send_rich_message_with_markup() -> None:
    bot = _FakeBot()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="x")]])

    await rich.send_rich(
        bot,  # type: ignore[arg-type]
        chat_id=123,
        markdown="# Hi",
        reply_markup=keyboard,
        fallback_text="Hi",
    )

    assert len(bot.posts) == 1
    endpoint, data = bot.posts[0]
    assert endpoint == "sendRichMessage"
    assert data == {
        "chat_id": 123,
        "rich_message": {"markdown": "# Hi"},
        "reply_markup": keyboard,
    }
    # No fallback when the rich send succeeds.
    assert bot.sent == []


async def test_send_rich_omits_reply_markup_when_none() -> None:
    bot = _FakeBot()
    await rich.send_rich(
        bot,  # type: ignore[arg-type]
        chat_id=1,
        markdown="x",
        fallback_text="x",
    )
    _endpoint, data = bot.posts[0]
    assert "reply_markup" not in data


async def test_send_rich_falls_back_to_plain_on_telegram_error() -> None:
    """A rejected rich payload (older client / API server) must fall
    back to a plain ``send_message`` so the reply is never dropped."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="x")]])
    bot = _FakeBot(post_side_effect=TelegramError("rich not supported"))

    await rich.send_rich(
        bot,  # type: ignore[arg-type]
        chat_id=42,
        markdown="# Rich",
        reply_markup=keyboard,
        fallback_text="Plain fallback",
        fallback_parse_mode="HTML",
    )

    # Attempted the rich send, then fell back.
    assert len(bot.posts) == 1
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == 42
    assert sent["text"] == "Plain fallback"
    assert sent["parse_mode"] == "HTML"
    assert sent["reply_markup"] is keyboard


async def test_edit_rich_posts_edit_message_text_with_ids() -> None:
    bot = _FakeBot()
    await rich.edit_rich(
        bot,  # type: ignore[arg-type]
        chat_id=5,
        message_id=9,
        markdown="# Edited",
    )
    endpoint, data = bot.posts[0]
    assert endpoint == "editMessageText"
    assert data == {
        "chat_id": 5,
        "message_id": 9,
        "rich_message": {"markdown": "# Edited"},
    }


async def test_send_thinking_draft_streams_thinking_text() -> None:
    """The 'Thinking…' bubble streams non-empty text via a plain
    ``sendMessageDraft`` — an empty draft renders as a literal "[]" on
    the client, and the rich ``<tg-thinking>`` block needs a custom
    emoji to show text."""
    bot = _FakeBot()
    await rich.send_thinking_draft(bot, chat_id=7, draft_id=42)  # type: ignore[arg-type]
    assert bot.posts == [
        ("sendMessageDraft", {"chat_id": 7, "draft_id": 42, "text": "Thinking…"}),
    ]


async def test_send_thinking_draft_swallows_telegram_error() -> None:
    """The bubble is cosmetic + best-effort — a Telegram-side failure
    (older API server, non-private chat) must not propagate."""
    bot = _FakeBot(post_side_effect=TelegramError("drafts unsupported"))
    # Must not raise.
    await rich.send_thinking_draft(bot, chat_id=1, draft_id=1)  # type: ignore[arg-type]
    assert len(bot.posts) == 1


async def test_edit_rich_propagates_telegram_error() -> None:
    """``edit_rich`` does NOT swallow errors — callers handle
    'not modified' / 'too long' themselves."""
    bot = _FakeBot(post_side_effect=BadRequest("Message is not modified"))
    with pytest.raises(BadRequest):
        await rich.edit_rich(
            bot,  # type: ignore[arg-type]
            chat_id=1,
            message_id=2,
            markdown="x",
        )
    # No fallback send on the edit path.
    assert bot.sent == []
