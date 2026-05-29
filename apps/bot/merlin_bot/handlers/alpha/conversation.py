"""``/alpha`` ConversationHandler factory.

Wires the freeform-first entry, the preview hub, and every inline-edit
step into one ``ConversationHandler``. Per-user (default).
"""

from __future__ import annotations

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from merlin_bot.handlers.alpha import (
    step_asset,
    step_conviction,
    step_direction,
    step_freeform,
    step_horizon,
    step_preview,
    step_thesis,
)
from merlin_bot.handlers.alpha.states import AlphaState


async def _cancel(update, _context):  # type: ignore[no-untyped-def]  # PTB callback shape
    """Conversation fallback — /cancel drops the draft and exits."""
    if update.message is not None:
        await update.message.reply_text("No problem. Hit /alpha when you've got something to call.")
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    """Build the ``/alpha`` ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("alpha", step_freeform.alpha_command),
        ],
        states={
            AlphaState.AWAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_freeform.receive_text),
            ],
            AlphaState.PREVIEW: [
                CallbackQueryHandler(
                    step_preview.on_confirm,
                    pattern=f"^{step_preview.CB_CONFIRM}$",
                ),
                CallbackQueryHandler(
                    step_preview.on_cancel,
                    pattern=f"^{step_preview.CB_CANCEL}$",
                ),
                CallbackQueryHandler(
                    step_asset.prompt_asset,
                    pattern=f"^{step_preview.CB_EDIT_ASSET}$",
                ),
                CallbackQueryHandler(
                    step_direction.prompt_direction,
                    pattern=f"^{step_preview.CB_EDIT_DIRECTION}$",
                ),
                CallbackQueryHandler(
                    step_horizon.prompt_horizon,
                    pattern=f"^{step_preview.CB_EDIT_HORIZON}$",
                ),
                CallbackQueryHandler(
                    step_conviction.prompt_conviction,
                    pattern=f"^{step_preview.CB_EDIT_CONVICTION}$",
                ),
                CallbackQueryHandler(
                    step_thesis.prompt_thesis,
                    pattern=f"^{step_preview.CB_EDIT_THESIS}$",
                ),
            ],
            AlphaState.EDIT_DIRECTION: [
                CallbackQueryHandler(step_direction.on_pick, pattern=r"^alpha:dir:"),
            ],
            AlphaState.EDIT_HORIZON: [
                CallbackQueryHandler(step_horizon.on_pick, pattern=r"^alpha:hor:"),
            ],
            AlphaState.EDIT_CONVICTION: [
                CallbackQueryHandler(step_conviction.on_pick, pattern=r"^alpha:conv:"),
            ],
            AlphaState.EDIT_THESIS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_thesis.receive_thesis),
            ],
            AlphaState.EDIT_ASSET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_asset.receive_asset_query),
            ],
            AlphaState.PICK_ASSET: [
                CallbackQueryHandler(step_asset.on_pick, pattern=r"^alpha:asset:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="alpha",
        persistent=False,
    )
