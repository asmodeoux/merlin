"""Thesis step — free-text input.

Used both for "Edit thesis" and as the form-flow's thesis step.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview
from merlin_bot.handlers.alpha.ui import render as ui_render


async def prompt_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Morph the preview card into a text-input prompt.

    No keyboard on this message — the user's reply is the text input;
    after ``receive_thesis`` runs, a fresh preview card is sent (we
    can't edit-in-place once the user has interleaved a text reply).
    """
    query = update.callback_query
    if query is not None:
        await query.answer()
    await ui_render(
        update,
        context,
        text="In your own words — what's the call?",
        reply_markup=None,
    )
    return AlphaState.EDIT_THESIS


async def receive_thesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture the thesis text and return to preview."""
    if update.message is None or context.user_data is None:
        return ConversationHandler.END
    thesis = (update.message.text or "").strip()
    if not thesis:
        await update.message.reply_text("Tell me what you think will happen.")
        return AlphaState.EDIT_THESIS
    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["thesis_text"] = thesis
    return await show_preview(update, context)
