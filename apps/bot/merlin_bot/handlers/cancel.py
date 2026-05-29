"""Shared ``/cancel`` command — works both inside ``/alpha`` and globally.

Sprint-1 live UX bug: ``/cancel`` was advertised in the slash-command
menu but only wired as a fallback inside the ``/alpha`` ConversationHandler.
If the user wasn't currently inside an ``/alpha`` conversation (e.g.
they'd already clicked the Cancel button and the conversation had
ended), typing ``/cancel`` matched no handler — Telegram showed the
command being sent and the bot stayed silent.

This handler is wired in **two** places:

* As the ``/alpha`` conversation's fallback (so ``/cancel`` ends the
  conversation cleanly when the user is mid-draft).
* As a global ``CommandHandler`` registered AFTER the ``/alpha``
  ConversationHandler at the application level (PTB only reaches a
  handler in a later position once earlier handlers decline to claim
  the update; so this global one fires only when the user is NOT in
  ``/alpha``).

Either way the user gets immediate feedback: the most recent inline
keyboard is stripped from chat, the in-memory draft is dropped, and a
one-line ack is sent.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    USER_KEY_DRAFT,
)
from merlin_bot.handlers.alpha.ui import strip_live_card_keyboard


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Universal ``/cancel`` handler. Always replies; never silently no-ops.

    Returns ``ConversationHandler.END`` so this same coroutine doubles
    as the ``/alpha`` fallback. When used as a global ``CommandHandler``
    the return value is ignored by PTB — harmless.
    """
    had_draft = bool(context.user_data and context.user_data.get(USER_KEY_DRAFT))

    # Strip the keyboard from the most recently rendered preview/picker
    # card BEFORE we drop the tracking keys. No-op when nothing's live.
    await strip_live_card_keyboard(context)

    if context.user_data is not None:
        for key in (USER_KEY_DRAFT, USER_KEY_ASSET_RESOLUTION, USER_KEY_CANDIDATES):
            context.user_data.pop(key, None)

    if update.message is not None:
        # Bot voice — friendly, never preachy. Two flavours so the user
        # gets a hint when they cancelled a real draft vs a no-op /cancel.
        ack = (
            "No problem. Hit /alpha when you've got something to call."
            if had_draft
            else "All clear — nothing to drop. Hit /alpha to start a new call."
        )
        await update.message.reply_text(ack)

    return ConversationHandler.END
