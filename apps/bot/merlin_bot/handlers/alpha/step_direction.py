"""Direction step — long / short.

Reachable from the preview's "Edit direction" button or as part of the
button form flow when the LLM couldn't extract direction.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview
from merlin_bot.handlers.alpha.ui import render as ui_render

CB_LONG = "alpha:dir:long"
CB_SHORT = "alpha:dir:short"


async def prompt_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Morph the preview card into the long/short picker."""
    query = update.callback_query
    if query is not None:
        await query.answer()
    await ui_render(
        update,
        context,
        text="Which way?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Long", callback_data=CB_LONG),
                    InlineKeyboardButton("Short", callback_data=CB_SHORT),
                ]
            ]
        ),
    )
    return AlphaState.EDIT_DIRECTION


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked long or short. Update the draft, jump back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None:
        return ConversationHandler.END
    await query.answer()
    value = "long" if query.data == CB_LONG else "short"
    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["direction"] = value
    return await show_preview(update, context)
