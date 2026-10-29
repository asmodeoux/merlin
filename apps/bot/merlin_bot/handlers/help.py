"""``/help`` handler — a 30-second tour of Merlin.

Stateless, always-on. Never enters a conversation; the user can hit
``/help`` from any state (including mid-``/alpha``) without disturbing
the draft they're working on. PTB routes the command to this handler
before the ConversationHandler matchers because ``CommandHandler``
sits in the default group and ConversationHandlers gate on state.

Strings follow the bot-voice rules: no version markers, no
internal terminology, confident present-tense framing.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

_log = logging.getLogger("merlin.bot.handlers.help")


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the help text. No state change, no DB call."""
    if update.message is None:
        return
    await update.message.reply_text(_help_text())


def build_handler() -> CommandHandler[ContextTypes.DEFAULT_TYPE, None]:
    """Wire the ``/help`` CommandHandler.

    Lives outside any ConversationHandler so it's reachable from every
    state. PTB v21 dispatches CommandHandlers ahead of state-matched
    MessageHandlers when the message starts with ``/``.
    """
    return CommandHandler("help", help_command)


def _help_text() -> str:
    """The 30-second tour. Returned as plain text — no markdown."""
    return (
        "Merlin in 30 seconds:\n"
        "\n"
        "/alpha — log a market call. Type freely "
        '("ETH long 2 weeks, Vitalik rumour") or tap through the buttons. '
        "I'll lock in the price and check back when the horizon is up.\n"
        "\n"
        "/me — your open calls, resolved calls, and hit rate.\n"
        "\n"
        "/board — who's calling it best across everyone.\n"
        "\n"
        "/invite — mint a fresh code to share with a friend. "
        "Single-use, good for 14 days.\n"
        "\n"
        "/cancel — drop the draft you're working on.\n"
        "\n"
        "A call, once you confirm it, is locked. That's the whole point — "
        "Merlin keeps an honest record. Edit a draft before you confirm, "
        "not after."
    )
