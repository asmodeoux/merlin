"""Unit tests for the shared ``/cancel`` handler.

Sprint-1 live UX bug: ``/cancel`` typed outside an ``/alpha``
conversation produced silent failure — the command was advertised in
the slash menu but had no global ``CommandHandler``. These tests pin:

* ``/cancel`` always replies, regardless of conversation state.
* Any stored preview-card keyboard is stripped before the ack.
* All ``/alpha`` user_data slots (draft, asset resolution, candidates,
  preview card pointers) are cleared.
* The ack copy differs between "you had a draft" and "you had nothing"
  so the user gets meaningful feedback either way.
* It's wired both as the ``/alpha`` ConversationHandler's fallback
  AND as a top-level ``CommandHandler`` on the application.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_bot.application import build_application
from merlin_bot.config import Settings
from merlin_bot.handlers.alpha.conversation import build_handler as build_alpha_handler
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    USER_KEY_DRAFT,
    USER_KEY_PREVIEW_CHAT_ID,
    USER_KEY_PREVIEW_MESSAGE_ID,
)
from merlin_bot.handlers.cancel import cancel_command
from telegram.error import BadRequest
from telegram.ext import CommandHandler

# The /alpha ConversationHandler uses ``per_message=False`` (mixed text +
# CallbackQuery flow); PTB emits a benign PTBUserWarning on build.
pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")


def _text_update(*, chat_id: int = 555, text: str = "/cancel") -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.effective_chat = MagicMock(id=chat_id)
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update.message = msg
    return update


def _context(**user_data: object) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.edit_message_reply_markup = AsyncMock()
    return ctx


async def test_cancel_with_active_draft_acks_and_clears() -> None:
    """Cancelling a real in-progress draft yields the "no problem"
    line, clears every alpha user_data slot, and strips the live
    keyboard so stale buttons can't fire from the back of the chat."""
    update = _text_update()
    ctx = _context(
        **{
            USER_KEY_DRAFT: {"direction": "long"},
            USER_KEY_ASSET_RESOLUTION: {"coingecko_id": "ethereum"},
            USER_KEY_CANDIDATES: [{"coingecko_id": "ethereum"}],
            USER_KEY_PREVIEW_CHAT_ID: 555,
            USER_KEY_PREVIEW_MESSAGE_ID: 1234,
        }
    )
    await cancel_command(update, ctx)
    # Ack sent.
    update.message.reply_text.assert_awaited_once()
    args, _ = update.message.reply_text.call_args
    assert "No problem" in args[0]
    # All alpha state cleared.
    assert USER_KEY_DRAFT not in ctx.user_data
    assert USER_KEY_ASSET_RESOLUTION not in ctx.user_data
    assert USER_KEY_CANDIDATES not in ctx.user_data
    assert USER_KEY_PREVIEW_CHAT_ID not in ctx.user_data
    assert USER_KEY_PREVIEW_MESSAGE_ID not in ctx.user_data
    # The stale keyboard got stripped.
    ctx.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=555, message_id=1234, reply_markup=None
    )


async def test_cancel_without_draft_uses_different_copy() -> None:
    """Cancelling when there's nothing to cancel still produces visible
    feedback — and the copy hints that nothing was actually dropped, so
    the user isn't misled into thinking they lost work."""
    update = _text_update()
    ctx = _context()
    await cancel_command(update, ctx)
    update.message.reply_text.assert_awaited_once()
    args, _ = update.message.reply_text.call_args
    assert "All clear" in args[0]
    # No live card → no edit attempt.
    ctx.bot.edit_message_reply_markup.assert_not_awaited()


async def test_cancel_tolerates_badrequest_when_stripping_keyboard() -> None:
    """If the live card is too old to edit (Telegram returns BadRequest),
    the user still gets the ack — the keyboard cleanup is best-effort."""
    update = _text_update()
    ctx = _context(
        **{
            USER_KEY_DRAFT: {"direction": "long"},
            USER_KEY_PREVIEW_CHAT_ID: 555,
            USER_KEY_PREVIEW_MESSAGE_ID: 1234,
        }
    )
    ctx.bot.edit_message_reply_markup.side_effect = BadRequest("message can't be edited")
    await cancel_command(update, ctx)
    # The ack still went out.
    update.message.reply_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Wiring contract — both ``/alpha`` fallback AND global ``CommandHandler``
# ---------------------------------------------------------------------------


def test_alpha_conversation_has_cancel_fallback() -> None:
    """Pin that ``/cancel`` is wired as a fallback INSIDE the
    ``/alpha`` ConversationHandler — without this, ``/cancel`` while
    mid-draft wouldn't drop the user out of the conversation."""
    handler = build_alpha_handler()
    cancel_fallbacks = [
        f for f in handler.fallbacks if isinstance(f, CommandHandler) and "cancel" in f.commands
    ]
    assert cancel_fallbacks, (
        "/alpha ConversationHandler must include CommandHandler('cancel', ...) "
        "as a fallback so users can drop out mid-draft."
    )


def test_application_registers_global_cancel_after_alpha() -> None:
    """Pin the registration ORDER. The global ``/cancel`` MUST come
    after the ``/alpha`` ConversationHandler, otherwise the global
    handler would fire ahead of the conversation's fallback (it doesn't
    in PTB's resolution model, but the convention keeps intent obvious
    to anyone reading ``application.py``)."""
    settings = Settings(
        telegram_bot_token="0:dummy",
        merlin_api_internal_token="x" * 32,
        merlin_api_base_url="http://localhost",
        telegram_admin_chat_ids=[],
    )
    app = build_application(settings)
    handlers = app.handlers[0]  # all in default group
    cancel_idx = next(
        i
        for i, h in enumerate(handlers)
        if isinstance(h, CommandHandler) and "cancel" in h.commands
    )
    # Find the ``/alpha`` ConversationHandler — it has ``name='alpha'``.
    alpha_idx = next(i for i, h in enumerate(handlers) if getattr(h, "name", None) == "alpha")
    assert cancel_idx > alpha_idx, (
        "Global /cancel CommandHandler must be registered AFTER the "
        "/alpha ConversationHandler so the conversation's own fallback "
        "claims in-conversation /cancel first."
    )
