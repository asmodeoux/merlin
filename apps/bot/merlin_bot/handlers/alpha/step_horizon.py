"""Horizon step — 24h / 7d / 14d / 30d / 90d."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview
from merlin_bot.handlers.alpha.ui import render as ui_render

CB_PREFIX = "alpha:hor:"

# User-facing label -> horizon code. The button label follows the
# bot-voice rule ("2 weeks", not "14d"). Keep the bot-voice translation
# in one place so future-tense and past-tense remain consistent.
_OPTIONS: list[tuple[str, str]] = [
    ("24 hours", "24h"),
    ("3 days", "3d"),
    ("1 week", "7d"),
    ("2 weeks", "14d"),
    ("1 month", "30d"),
    ("3 months", "90d"),
]


async def prompt_horizon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Morph the preview card into the horizon picker."""
    query = update.callback_query
    if query is not None:
        await query.answer()
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{CB_PREFIX}{code}")]
        for label, code in _OPTIONS
    ]
    await ui_render(
        update,
        context,
        text="How long until you find out?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AlphaState.EDIT_HORIZON


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the picked horizon code; back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None or not query.data:
        return ConversationHandler.END
    await query.answer()
    code = query.data.removeprefix(CB_PREFIX)
    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["horizon"] = code
    return await show_preview(update, context)
