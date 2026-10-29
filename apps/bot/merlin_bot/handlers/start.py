"""``/start`` handler — onboarding flow.

Three states:

1. **Returning user.** TG user already has a redeemed invite → bot
   greets, shows the welcome menu (suggestions for ``/alpha``, ``/me``,
   ``/board``), waits for the next message.
2. **New user with no invite in conversation memory.** Bot asks for an
   invite code. The user pastes it; bot calls
   ``MerlinClient.redeem_invite`` and, on success, moves to state 3.
3. **Just-redeemed.** Bot asks for a display name (the leaderboard
   entry). The user replies; bot calls ``MerlinClient.update_me`` and
   greets them.

Strings follow the bot-voice rules: no version markers, no
internal terminology, calendar-friendly time references where they
appear, confident present-tense framing.
"""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.start")


class StartState(IntEnum):
    """Conversation states for the onboarding flow."""

    AWAIT_INVITE = 1
    AWAIT_DISPLAY_NAME = 2


# user_data keys
_USER_KEY_SOURCE_ID = "source_id"
_USER_KEY_DISPLAY_NAME = "display_name"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for ``/start``. Greet, admin-bootstrap, or ask for invite."""
    if update.effective_user is None or update.message is None:
        return ConversationHandler.END

    if context.user_data is not None and context.user_data.get(_USER_KEY_SOURCE_ID):
        await update.message.reply_text(_returning_user_greeting(context.user_data))
        return ConversationHandler.END

    # Admin bypass: if the calling Telegram id is in the allowlist, skip
    # the invite-code prompt entirely. We INSERT (or fetch) the admin's
    # source row via ``POST /sources/admin_bootstrap`` and jump straight
    # to the display-name step.
    admin_chat_ids: frozenset[int] = context.bot_data.get(
        BOT_DATA_ADMIN_KEY,
        frozenset(),
    )
    if update.effective_user.id in admin_chat_ids:
        return await _admin_bootstrap(update, context)

    await update.message.reply_text(_welcome_text())
    return StartState.AWAIT_INVITE


async def _admin_bootstrap(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Create or fetch the admin's source row, then prompt for display name.

    On API error we log and fall through to the invite-code prompt —
    the admin can still onboard the normal way if the bootstrap path
    is broken, which is far better than locking them out entirely.
    """
    assert update.effective_user is not None and update.message is not None

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.admin_bootstrap(
            tg_user_id=update.effective_user.id,
            tg_handle=update.effective_user.username,
        )
    except MerlinHTTPError as e:
        _log.warning("admin_bootstrap failed (%s); falling back to invite flow", e)
        await update.message.reply_text(_welcome_text())
        return StartState.AWAIT_INVITE

    if context.user_data is not None:
        context.user_data[_USER_KEY_SOURCE_ID] = result.get("source_id")

    if result.get("display_name_prompt", True):
        await update.message.reply_text(_admin_welcome_text())
        return StartState.AWAIT_DISPLAY_NAME

    await update.message.reply_text(_welcome_done_text(display_name=None))
    return ConversationHandler.END


async def receive_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent us their invite code; try to redeem it."""
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    code = (update.message.text or "").strip()
    if not code:
        await update.message.reply_text("Paste your invite code to get started.")
        return StartState.AWAIT_INVITE

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.redeem_invite(
            code=code,
            tg_user_id=update.effective_user.id,
            tg_handle=update.effective_user.username,
        )
    except MerlinHTTPError as e:
        if e.status_code == 404:
            await update.message.reply_text(_invite_invalid_text())
            return StartState.AWAIT_INVITE
        _log.warning("invite redemption failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return StartState.AWAIT_INVITE

    if context.user_data is not None:
        context.user_data[_USER_KEY_SOURCE_ID] = result.get("source_id")
    if result.get("display_name_prompt", True):
        await update.message.reply_text(_ask_display_name_text())
        return StartState.AWAIT_DISPLAY_NAME
    await update.message.reply_text(_welcome_done_text(display_name=None))
    return ConversationHandler.END


async def receive_display_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent us their display name; save it and greet them."""
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    # 40-character soft cap matches the contract (``UpdateMeRequest``).
    display_name = raw[:40]
    if not display_name:
        await update.message.reply_text("What name should we put on the leaderboard?")
        return StartState.AWAIT_DISPLAY_NAME

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        await client.update_me(
            display_name=display_name,
            tg_user_id=update.effective_user.id,
        )
    except MerlinHTTPError as e:
        _log.warning("update_me failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return StartState.AWAIT_DISPLAY_NAME

    if context.user_data is not None:
        context.user_data[_USER_KEY_DISPLAY_NAME] = display_name
    await update.message.reply_text(_welcome_done_text(display_name=display_name))
    return ConversationHandler.END


async def cancel_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/cancel`` — bail out of the conversation."""
    if update.message is not None:
        await update.message.reply_text("No problem. Hit /start whenever you're ready.")
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    """Wire the onboarding ConversationHandler.

    The conversation is per-user (``per_user=True`` by default). Display
    name input and invite input are both plain text; admins can
    bypass via ``/admin/*`` commands which live in their own handlers.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            StartState.AWAIT_INVITE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite),
            ],
            StartState.AWAIT_DISPLAY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_display_name),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="onboarding",
        persistent=False,
    )


# ---------------------------------------------------------------------------
# Bot-voice strings — no version markers, no internal jargon, friendly tone.
# ---------------------------------------------------------------------------


def _welcome_text() -> str:
    """Initial /start greeting for new users."""
    return "Welcome. Paste your invite code to get started."


def _ask_display_name_text() -> str:
    """Prompt after a successful invite redemption."""
    return "You're in. What name should we put on the leaderboard?"


def _admin_welcome_text() -> str:
    """First-time admin greeting after auto-bootstrap (no invite needed)."""
    return (
        "Welcome, operator. What name should we put on the leaderboard? "
        "You can use /invite to issue codes."
    )


def _welcome_done_text(*, display_name: str | None) -> str:
    """Final onboarding message — invites the user to make their first call."""
    name = display_name or "there"
    return (
        f"You're in, {name}. Tell me what you think will happen — "
        f"or use /alpha if you'd rather tap through."
    )


def _returning_user_greeting(user_data: dict[str, Any]) -> str:
    """Greeting for an already-onboarded user who hits /start again."""
    name = user_data.get(_USER_KEY_DISPLAY_NAME, "there")
    return (
        f"Welcome back, {name}. Tell me what you think will happen, "
        f"or use /me to see your open calls."
    )


def _invite_invalid_text() -> str:
    """User pasted a code that doesn't validate."""
    return (
        "That code doesn't look right — it might already be used or expired. "
        "Double-check and try again, or reach out if you need a new one."
    )


def _temporary_error_text() -> str:
    """Recoverable error; ask the user to try again."""
    return "Something's off on our end. Try again in a moment."
