"""``/invite`` — admin command for issuing fresh invite codes.

Flow: admin types ``/invite`` → bot checks the admin allowlist → calls
``POST /invites`` on the API → replies with the freshly minted code +
expiry. Non-admins silently get a "command not found" style reply so
the operator surface stays hidden.

The code returned is the *only* thing the admin shares with the
invitee — pasting it into the bot from a different Telegram account
hits ``POST /invites/redeem`` and the user is onboarded.
"""

from __future__ import annotations

import logging

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.admin.invite")


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/invite`` — mint a fresh invite code and reply with it."""
    if update.effective_user is None or update.message is None:
        return

    admin_chat_ids: frozenset[int] = context.bot_data.get(
        BOT_DATA_ADMIN_KEY,
        frozenset(),
    )
    if update.effective_user.id not in admin_chat_ids:
        # Silent for non-admins — same shape as Telegram's "unknown
        # command" reply for a typo'd /<anything>. Logging here would
        # be noisy and pointless (anyone scanning slash commands).
        await update.message.reply_text(_unknown_command_text())
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.create_invite(
            tg_user_id=update.effective_user.id,
            expires_in_days=14,
        )
    except MerlinHTTPError as e:
        _log.warning("create_invite failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    code = result.get("code", "")
    expires_at = result.get("expires_at", "")
    await update.message.reply_text(
        _invite_created_text(code=code, expires_at=expires_at),
        parse_mode="MarkdownV2",
    )


def build_handler() -> CommandHandler:
    """Wire the ``/invite`` command handler."""
    return CommandHandler("invite", invite_command)


# ---------------------------------------------------------------------------
# Bot-voice strings.
# ---------------------------------------------------------------------------


def _invite_created_text(*, code: str, expires_at: str) -> str:
    """Format the response a freshly minted invite is pasted into.

    MarkdownV2-escaped code block — the code is the only thing the
    admin needs to copy out. Calendar-friendly expiry (the ISO
    timestamp is fine here because this is an internal/admin message,
    not a user-facing one).
    """
    safe_expiry = _md2_escape(expires_at)
    safe_code = _md2_escape(code)
    return f"New invite ready\\.\n\n`{safe_code}`\n\nExpires {safe_expiry}\\."


def _temporary_error_text() -> str:
    return "Couldn't mint a code right now. Try again in a moment."


def _unknown_command_text() -> str:
    """Reply to a non-admin who types ``/invite``.

    Mirrors Telegram's generic unknown-command tone. We don't say "you
    aren't an admin" — that would just confirm the operator surface
    exists.
    """
    return "I don't know that command. Try /help."


def _md2_escape(text: str) -> str:
    """Escape MarkdownV2 reserved characters.

    Telegram's MarkdownV2 reserves these for formatting:
    ``_ * [ ] ( ) ~ ` > # + - = | { } . !``
    Anywhere they appear in literal content (including the code body
    or the timestamp), they have to be backslash-prefixed.
    """
    reserved = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in reserved else c for c in text)
