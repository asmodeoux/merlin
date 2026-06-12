"""Unit tests for the ``/tickers`` handler.

Three things to pin:

1. Rendering: each per-ticker entry lands on its own bullet line
   with the agreed chunk order (PnL, hit, calls). The user-facing
   numbers match the API payload byte-for-byte.
2. Empty-state copy: zero entries reads "Tracking your first
   resolutions" rather than a blank list. Never a misleading 0%.
3. Error paths: 404 from the API → /start hint; other failures →
   recoverable temporary-error copy. No raw stacktraces leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.tickers import (
    _format_calls_chunk,
    _format_hit_chunk,
    _format_pnl_chunk,
    _render_tickers,
    _render_tickers_rich,
    build_handler,
    tickers_command,
)
from merlin_client.errors import MerlinHTTPError
from telegram.error import TelegramError
from telegram.ext import CommandHandler

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
    """Captures raw ``_post`` (rich send) + ``send_message`` (fallback).

    ``post_side_effect`` lets a test force the rich send to fail so the
    ``send_rich`` plain-text fallback path is exercised.
    """

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


def test_render_tickers_with_entries_one_row_per_ticker() -> None:
    """Each entry lands on its own bullet line with chunk order:
    ``• ASSET — PnL +X.X% · YY% hit · N calls``."""
    payload = {
        "entries": [
            {
                "asset_symbol": "TON",
                "calls_resolved": 2,
                "calls_correct": 1,
                "accuracy_pct": 50.0,
                "weighted_pnl_pct": 6.1,
            },
            {
                "asset_symbol": "BTC",
                "calls_resolved": 8,
                "calls_correct": 2,
                "accuracy_pct": 25.0,
                "weighted_pnl_pct": -1.3,
            },
        ],
    }

    text = _render_tickers(payload)

    assert text.startswith("Your tickers")
    # Ordering caption explains why the rows are sorted the way they
    # are — without it a +20% (1-call) row sitting below a +6%
    # (4-call) row reads as random. Pinned so a refactor that drops
    # the caption is loud.
    assert "Sorted by track-record-weighted PnL." in text
    assert "• TON — PnL +6.1% · 50% hit · 2 calls" in text
    assert "• BTC — PnL -1.3% · 25% hit · 8 calls" in text
    # Footer call-to-action.
    assert "/alpha" in text
    assert "/history" in text


def test_render_tickers_empty_state_collapses_to_cold_start_copy() -> None:
    """Cold-start source: no entries → the honest empty-state copy.
    No bullet lines, no zero placeholders."""
    text = _render_tickers({"entries": []})
    assert "Tracking your first resolutions" in text
    assert "•" not in text


def test_render_tickers_handles_missing_entries_key() -> None:
    """Defensive: a wire-shape drift that drops ``entries`` entirely
    must NOT crash. Treated as the cold-start empty-state."""
    text = _render_tickers({})
    assert "Tracking your first resolutions" in text


def test_render_tickers_asset_symbol_upcased() -> None:
    """A ticker symbol that arrives lowercased (wire-shape drift)
    still renders in all caps — matches ``/me`` and ``/board``."""
    payload = {
        "entries": [
            {
                "asset_symbol": "ton",
                "calls_resolved": 1,
                "calls_correct": 1,
                "accuracy_pct": 100.0,
                "weighted_pnl_pct": 5.0,
            },
        ],
    }
    text = _render_tickers(payload)
    assert "• TON — " in text


def test_format_pnl_chunk_signs_positive_and_negative() -> None:
    """Signed % with one decimal: ``+6.1%`` / ``-1.3%`` / ``+0.0%``."""
    assert _format_pnl_chunk(6.1) == "PnL +6.1%"
    assert _format_pnl_chunk(-1.3) == "PnL -1.3%"
    assert _format_pnl_chunk(0.0) == "PnL +0.0%"


def test_format_pnl_chunk_falls_back_on_none_and_garbage() -> None:
    """Em-dash on None / non-numeric — never crash on bad input."""
    assert _format_pnl_chunk(None) == "— PnL"
    assert _format_pnl_chunk("not-a-number") == "— PnL"
    assert _format_pnl_chunk(object()) == "— PnL"


def test_format_hit_chunk_integer_percent() -> None:
    """``50% hit`` — no decimals, matches /board."""
    assert _format_hit_chunk(50.0) == "50% hit"
    assert _format_hit_chunk(100.0) == "100% hit"
    assert _format_hit_chunk(0.0) == "0% hit"


def test_format_hit_chunk_falls_back_on_none_and_garbage() -> None:
    """Em-dash on None / non-numeric — same defensive pattern as PnL."""
    assert _format_hit_chunk(None) == "— hit"
    assert _format_hit_chunk("nope") == "— hit"


def test_format_calls_chunk_pluralises() -> None:
    """Singular ``1 call``, plural ``2 calls``."""
    assert _format_calls_chunk(1) == "1 call"
    assert _format_calls_chunk(2) == "2 calls"
    assert _format_calls_chunk(0) == "0 calls"


def test_format_calls_chunk_falls_back_on_non_numeric() -> None:
    """Em-dash on non-numeric drift — the wire shape guarantees an
    int but a defensive renderer keeps the message intact."""
    assert _format_calls_chunk(None) == "— calls"
    assert _format_calls_chunk("two") == "— calls"


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_tickers_command_fetches_and_renders() -> None:
    """Happy path: /tickers hits the API with caller's tg_user_id,
    then sends the rendered payload as a Rich Message (sendRichMessage)
    targeting the caller's chat."""
    client = AsyncMock()
    client.get_my_tickers.return_value = {
        "entries": [
            {
                "asset_symbol": "TON",
                "calls_resolved": 2,
                "calls_correct": 1,
                "accuracy_pct": 50.0,
                "weighted_pnl_pct": 6.1,
            },
        ],
    }
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    bot = _FakeBot()
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    client.get_my_tickers.assert_awaited_once_with(tg_user_id=USER_ID)
    # One rich send, no plain reply.
    assert update.message is not None
    assert update.message.replies == []
    assert len(bot.posts) == 1
    endpoint, data = bot.posts[0]
    assert endpoint == "sendRichMessage"
    assert data["chat_id"] == CHAT_ID
    markdown = data["rich_message"]["markdown"]
    assert "# Your tickers" in markdown
    assert "| TON | +6.1% | 50% | 2 |" in markdown


async def test_tickers_command_falls_back_to_plain_when_rich_rejected() -> None:
    """If Telegram rejects the rich payload (older client / API server),
    ``send_rich`` re-sends the plain-text render via ``send_message`` so
    the user still gets their breakdown."""
    client = AsyncMock()
    client.get_my_tickers.return_value = {
        "entries": [
            {
                "asset_symbol": "TON",
                "calls_resolved": 2,
                "calls_correct": 1,
                "accuracy_pct": 50.0,
                "weighted_pnl_pct": 6.1,
            },
        ],
    }
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    bot = _FakeBot(post_side_effect=TelegramError("rich not supported"))
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client}, bot=bot)

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    # Rich send was attempted then fell back to a plain send_message.
    assert len(bot.posts) == 1
    assert len(bot.sent) == 1
    sent_chat, sent_text, _kw = bot.sent[0]
    assert sent_chat == CHAT_ID
    # The fallback carries the legacy plain render (bullet rows).
    assert "• TON — PnL +6.1% · 50% hit · 2 calls" in sent_text


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def test_render_tickers_rich_builds_table_with_heading() -> None:
    """The rich render is a heading + a four-column table; numbers land
    in their own right-aligned columns."""
    payload = {
        "entries": [
            {
                "asset_symbol": "TON",
                "calls_resolved": 2,
                "accuracy_pct": 50.0,
                "weighted_pnl_pct": 6.1,
            },
            {
                "asset_symbol": "btc",
                "calls_resolved": 8,
                "accuracy_pct": 25.0,
                "weighted_pnl_pct": -1.3,
            },
        ],
    }
    text = _render_tickers_rich(payload)
    assert text.startswith("# Your tickers")
    assert "| Ticker | PnL | Hit | Calls |" in text
    # Symbol upcased; one row per ticker; numbers bare (no inline label).
    assert "| TON | +6.1% | 50% | 2 |" in text
    assert "| BTC | -1.3% | 25% | 8 |" in text


def test_render_tickers_rich_empty_state_has_no_table() -> None:
    """Cold-start: the rich render falls back to the plain empty-state
    copy (no table to align)."""
    text = _render_tickers_rich({"entries": []})
    assert "Tracking your first resolutions" in text
    assert "|" not in text


def test_render_tickers_rich_escapes_pipe_in_symbol() -> None:
    """A wire-drift asset symbol containing a pipe must be escaped so it
    can't shear the table into a phantom column."""
    text = _render_tickers_rich(
        {
            "entries": [
                {
                    "asset_symbol": "a|b",
                    "calls_resolved": 1,
                    "accuracy_pct": 100.0,
                    "weighted_pnl_pct": 5.0,
                },
            ],
        },
    )
    assert r"A\|B" in text


async def test_tickers_command_404_points_at_start() -> None:
    """User typed /tickers but never finished /start → /start hint."""
    client = AsyncMock()
    client.get_my_tickers.side_effect = MerlinHTTPError(404, "source not found")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "/start" in body


async def test_tickers_command_recoverable_error_reply() -> None:
    """A 5xx → recoverable copy. No raw status codes / error bodies leak."""
    client = AsyncMock()
    client.get_my_tickers.side_effect = MerlinHTTPError(503, "down")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "moment" in body.lower() or "try" in body.lower()
    assert "503" not in body
    assert "down" not in body.lower()


async def test_tickers_command_recovers_from_transport_failure() -> None:
    """Non-HTTP exceptions (DNS, timeout) fall into the broader
    ``except Exception`` branch — recoverable copy, no stacktrace."""
    client = AsyncMock()
    client.get_my_tickers.side_effect = RuntimeError("dns resolution failed")
    update = _FakeUpdate(
        effective_user=_FakeUser(id=USER_ID),
        message=_FakeMessage(),
    )
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "moment" in body.lower() or "try" in body.lower()
    assert "RuntimeError" not in body


async def test_tickers_command_handles_missing_message_silently() -> None:
    """No ``Update.message`` → short-circuit (channel post / inline mode)."""
    client = AsyncMock()
    update = _FakeUpdate(effective_user=_FakeUser(id=USER_ID), message=None)
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    client.get_my_tickers.assert_not_called()


async def test_tickers_command_handles_missing_effective_user_silently() -> None:
    """No ``effective_user`` → no-op. Don't call the API without a caller id."""
    client = AsyncMock()
    update = _FakeUpdate(effective_user=None, message=_FakeMessage())
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await tickers_command(update, ctx)  # type: ignore[arg-type]

    client.get_my_tickers.assert_not_called()
    assert update.message is not None
    assert update.message.replies == []


# ---------------------------------------------------------------------------
# Build_handler
# ---------------------------------------------------------------------------


def test_build_handler_returns_command_handler_for_tickers() -> None:
    handler = build_handler()
    assert isinstance(handler, CommandHandler)
    assert "tickers" in handler.commands


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")
def test_tickers_handler_registered_in_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot's ``build_application`` must include the ``/tickers``
    handler — otherwise the slash menu lies and the command falls
    through to silence."""
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
    assert "tickers" in commands_registered
