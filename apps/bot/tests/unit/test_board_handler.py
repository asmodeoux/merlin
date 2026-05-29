"""Unit tests for the ``/board`` handler.

Today the API returns an empty list (no outcomes layer yet). The
handler must render an honest empty-state — not a blank message, not
"coming soon". The rendering for the populated path is also pinned so
the swap-in is just the API change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.board import (
    _empty_state_text,
    _render_board,
    board_command,
    build_handler,
)
from merlin_client.errors import MerlinHTTPError
from telegram.ext import CommandHandler
from telegram.warnings import PTBUserWarning

_ = PTBUserWarning  # re-exported so the type checker keeps the symbol


@dataclass
class _FakeMessage:
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def reply_text(self, text: str, **kw: Any) -> None:
        self.replies.append((text, kw))


@dataclass
class _FakeUpdate:
    message: _FakeMessage | None


@dataclass
class _FakeContext:
    bot_data: dict[str, Any]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_empty_state_text_frames_as_product_choice() -> None:
    """The honest empty-state — no "coming soon" / "v0" / version
    markers. Reads as a product decision."""
    text = _empty_state_text()
    assert "Leaderboard unlocks" in text
    assert "/alpha" in text
    assert "/me" in text
    # Bot voice — no forbidden tokens.
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


def test_render_board_with_rows_renders_ranked_list() -> None:
    """Forward-compat: when the API returns rows, render rank + name
    + accuracy + resolved count, top-N only."""
    payload = {
        "window": "all_time",
        "entries": [
            {
                "rank": 1,
                "display_name": "Alice",
                "accuracy_pct": 80.0,
                "calls_resolved": 10,
                "calls_correct": 8,
            },
            {
                "rank": 2,
                "display_name": "Bob",
                "accuracy_pct": 57.0,
                "calls_resolved": 7,
                "calls_correct": 4,
            },
        ],
    }
    text = _render_board(payload)
    assert "Leaderboard:" in text
    assert "1. Alice — 80% (10 resolved)" in text
    assert "2. Bob — 57% (7 resolved)" in text
    assert "/alpha" in text
    assert "/me" in text


def test_render_board_trims_to_top_n() -> None:
    """A flood of entries gets trimmed to ``TOP_N`` to keep the
    message short — Telegram has a 4096-char cap, and a 50-row table
    is unhelpful UX anyway."""
    from merlin_bot.handlers.board import TOP_N

    entries = [
        {
            "rank": i + 1,
            "display_name": f"User{i}",
            "accuracy_pct": 50.0 + i,
            "calls_resolved": 1,
            "calls_correct": 1,
        }
        for i in range(TOP_N + 10)
    ]
    text = _render_board({"entries": entries, "window": "all_time"})
    # Last shown entry is rank TOP_N; rank TOP_N+1 is NOT in the
    # output.
    assert f"{TOP_N}. User{TOP_N - 1}" in text
    assert f"{TOP_N + 1}. User{TOP_N}" not in text


def test_render_board_avoids_internal_jargon() -> None:
    """Bot voice — no Bayesian / regret / belief score leaks even on
    the populated path."""
    payload = {
        "window": "all_time",
        "entries": [
            {
                "rank": 1,
                "display_name": "Alice",
                "accuracy_pct": 80.0,
                "calls_resolved": 10,
                "calls_correct": 8,
            },
        ],
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
        "source ",
    )
    for term in forbidden:
        assert term not in text, f"/board text leaks {term!r}"


def test_render_board_handles_null_accuracy_gracefully() -> None:
    """If accuracy is null on a row (edge case during transition),
    render an em-dash rather than crashing."""
    payload = {
        "window": "all_time",
        "entries": [
            {
                "rank": 1,
                "display_name": "Alice",
                "accuracy_pct": None,
                "calls_resolved": 0,
                "calls_correct": 0,
            },
        ],
    }
    text = _render_board(payload)
    assert "1. Alice — — (0 resolved)" in text


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


async def test_board_command_hits_api_and_replies() -> None:
    """/board calls get_board(window=all_time) and renders the payload."""
    client = AsyncMock()
    client.get_board.return_value = {"entries": [], "window": "all_time"}
    update = _FakeUpdate(message=_FakeMessage())
    ctx = _FakeContext(bot_data={BOT_DATA_KEY: client})

    await board_command(update, ctx)  # type: ignore[arg-type]

    client.get_board.assert_awaited_once_with(window="all_time")
    assert update.message is not None
    body, _ = update.message.replies[0]
    assert "Leaderboard unlocks" in body


async def test_board_command_temporary_error_on_5xx() -> None:
    client = AsyncMock()
    client.get_board.side_effect = MerlinHTTPError(503, "down")
    update = _FakeUpdate(message=_FakeMessage())
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
