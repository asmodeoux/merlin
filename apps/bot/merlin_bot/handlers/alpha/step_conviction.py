"""Conviction step — weak / medium / strong / very strong.

No "simulated size" leakage. The user picks one of four words; the
internal $-mapping (if any) lives entirely server-side.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview

CB_PREFIX = "alpha:conv:"

_OPTIONS: list[tuple[str, str]] = [
    ("Weak", "weak"),
    ("Medium", "medium"),
    ("Strong", "strong"),
    ("Very strong", "very_strong"),
]


async def prompt_conviction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show conviction buttons."""
    query = update.callback_query
    if query is not None:
        await query.answer()
    if update.effective_chat is None:
        return ConversationHandler.END
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{CB_PREFIX}{code}")]
        for label, code in _OPTIONS
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="How sure are you?",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AlphaState.EDIT_CONVICTION


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the picked conviction; back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None or not query.data:
        return ConversationHandler.END
    await query.answer()
    code = query.data.removeprefix(CB_PREFIX)
    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["conviction"] = code
    return await show_preview(update, context)
