"""``/alpha`` ConversationHandler factory.

Wires the freeform-first entry, the preview hub, and every inline-edit
step into one ``ConversationHandler``. Per-user (default).
"""

from __future__ import annotations

from typing import Any

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

# Seconds of inactivity before an abandoned ``/alpha`` conversation
# auto-ends. Ten minutes is calibrated against the realistic
# asset-picker flow: a user mid-pick may legitimately need to look
# up a contract address, copy from an explorer, or research a long-
# tail ticker before sending the next message. The previous 120s
# floor was too tight — users routinely crossed it while pasting a
# fresh address, and the next message they sent then bypassed the
# in-conversation EDIT_ASSET handler and got intercepted by the
# freeform-text entry-point, firing the LLM extractor on what the
# user thought was a follow-up search. ``/cancel`` remains available
# at any point if the user really did abandon the draft, so a
# longer ceiling has no downside.
ALPHA_CONVERSATION_TIMEOUT_SECONDS = 600


def _resolve_cancel_command() -> Any:
    """Late import the shared ``/cancel`` handler.

    Dodges the circular ``cancel.py`` ↔ ``alpha/__init__.py`` ↔
    ``conversation.py`` loop. Called once from ``build_handler()`` at
    application startup so the per-call cost is zero.
    """
    from merlin_bot.handlers.cancel import cancel_command

    return cancel_command


def build_handler() -> ConversationHandler:
    """Build the ``/alpha`` ConversationHandler.

    Two entry points:

    * ``/alpha`` command — explicit invocation, optionally with trailing
      text the user typed on the same line.
    * Any plain text from an idle user — Q-028's "tell me what you
      think" promise. When no other conversation is active, the user
      can just type their call (e.g. "ETH long 2w, Vitalik staking
      news") and the bot pipes it through the LLM extractor straight
      to the preview hub. PTB evaluates entry_points only when the
      user has no active state across ANY ConversationHandler — so
      this won't intercept the ``/start`` onboarding's display-name
      reply or the in-conversation freeform / thesis edit steps.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("alpha", step_freeform.alpha_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND, step_freeform.receive_text),
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
                # Free-form text reply ("3 hours", "1.5 days", "2 weeks")
                # so the user is never trapped at one of the preset
                # quick-picks. Parser + range validation live in the
                # step; failures re-render the picker with a hint.
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    step_horizon.receive_horizon_text,
                ),
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
                # Mid-pick text reply: pasted CoinGecko URL → resolve
                # directly; otherwise re-render the picker as a fresh
                # message below the live one (mirrors the free-form
                # horizon-text pattern in step_horizon). Lets users
                # refine without having to abandon the conversation.
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    step_asset.on_text_during_pick,
                ),
            ],
        },
        # Late import — ``merlin_bot.handlers.cancel`` imports from
        # ``alpha.states`` which triggers loading this package's
        # ``__init__.py`` (which loads this module). Resolving the
        # cancel handler at build time, not at import time, breaks the
        # cycle. ``build_handler()`` is called once at startup, so this
        # has no runtime cost.
        fallbacks=[CommandHandler("cancel", _resolve_cancel_command())],
        name="alpha",
        persistent=False,
        # Auto-end abandoned conversations after a period of
        # inactivity so a user who hits ``/alpha`` and walks away
        # doesn't keep their AWAIT_TEXT slot indefinitely. Without
        # this, every subsequent message they send (a chat in another
        # context, a typo, anything) gets intercepted by the freeform
        # handler and fires the LLM extractor on out-of-context input.
        # PTB's ``conversation_timeout`` requires the JobQueue extra,
        # which the bot already pulls in via
        # ``python-telegram-bot[..., job-queue, ...]``.
        conversation_timeout=ALPHA_CONVERSATION_TIMEOUT_SECONDS,
    )
