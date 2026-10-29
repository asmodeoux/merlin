"""Unit tests for the /help handler text + handler factory.

Verifies the help text obeys the bot-voice rules and doesn't leak
internal terminology, and that ``build_handler`` returns a usable
CommandHandler bound to the ``help`` command.
"""

from __future__ import annotations

import pytest
from merlin_bot.handlers.help import _help_text, build_handler
from telegram.ext import CommandHandler


def test_help_text_mentions_every_user_command() -> None:
    text = _help_text()
    for cmd in ("/alpha", "/me", "/board", "/cancel"):
        assert cmd in text, f"help text must reference {cmd}"


def test_help_text_avoids_internal_terms() -> None:
    """Bot voice — no internal jargon leaks in the help."""
    text = _help_text().lower()
    forbidden = (
        "source",
        "claim",
        "hypothesis",
        "event",
        "outcome",
        "regret",
        "canonical form",
        "simulated",
        "v0",
        "mvp",
        "phase 2",
        "pending",
        "freeform_llm",
        "not scored",
    )
    for term in forbidden:
        assert term not in text, f"help text leaks internal term: {term!r}"


def test_help_text_uses_calendar_language_not_codes() -> None:
    """No horizon codes like ``14d`` should appear; user-facing copy uses
    calendar phrasing (e.g. ``2 weeks``)."""
    text = _help_text()
    for code in ("14d", "30d", "90d", "1h", "4h", "24h", "3d", "7d"):
        assert code not in text, f"help text leaks horizon code: {code!r}"


def test_help_text_is_plain_text() -> None:
    """No MarkdownV2 reserved characters that would trip parsing."""
    text = _help_text()
    # Backticks and asterisks would render as inline-code / bold in
    # MarkdownV2 — we ship plain text only.
    assert "`" not in text
    assert "**" not in text
    # The text is short enough to fit a single Telegram message
    # (4096-char cap).
    assert len(text) < 4096


@pytest.mark.parametrize(
    "phrase",
    [
        "Merlin in 30 seconds",
        "log a market call",
        "honest record",
    ],
)
def test_help_text_includes_signature_phrasing(phrase: str) -> None:
    assert phrase in _help_text()


def test_build_handler_returns_command_handler_for_help() -> None:
    handler = build_handler()
    assert isinstance(handler, CommandHandler)
    assert "help" in handler.commands
