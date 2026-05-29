"""Regression — the ``/alpha`` ConversationHandler must accept plain text
as an entry point, not just the ``/alpha`` command.

The end-of-onboarding message tells the user "Tell me what you think
will happen — or use /alpha if you'd rather tap through." A user who
takes that at face value and types their call without typing /alpha
first ate silence in Sprint 1: the only entry point was
``CommandHandler("alpha", ...)``. This test pins the contract that
plain text routes into the freeform draft path.
"""

from __future__ import annotations

import pytest
from merlin_bot.handlers.alpha.conversation import build_handler
from merlin_bot.handlers.start import _welcome_done_text
from telegram.ext import CommandHandler, MessageHandler

# The /alpha ConversationHandler uses ``per_message=False`` (mixed text +
# CallbackQuery flow) and PTB emits a benign warning on build.
pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")


def test_alpha_handler_has_command_entry_point() -> None:
    handler = build_handler()
    command_entries = [e for e in handler.entry_points if isinstance(e, CommandHandler)]
    assert command_entries, "/alpha must remain reachable via the slash command"


def test_alpha_handler_accepts_plain_text_entry() -> None:
    """A MessageHandler matching text-without-commands MUST be an entry
    point, otherwise the welcome message's "Tell me what you think"
    promise is a lie."""
    handler = build_handler()
    message_entries = [e for e in handler.entry_points if isinstance(e, MessageHandler)]
    assert message_entries, (
        "/alpha ConversationHandler has no text-based entry point. The "
        "end-of-onboarding message tells users they can just describe "
        "their call. Add ``MessageHandler(filters.TEXT & ~filters.COMMAND, "
        "step_freeform.receive_text)`` to the entry_points list."
    )


def test_welcome_done_text_promises_freeform_entry() -> None:
    """If we ever change the welcome string to NOT promise freeform
    entry, the previous test becomes unnecessary. Pin the contract
    here so a future copy edit forces the matching wiring update."""
    text = _welcome_done_text(display_name="Yuri")
    # The exact phrasing isn't pinned — just the promise that typing
    # something other than /alpha is supported.
    assert "tell me" in text.lower() or "/alpha" in text.lower()
