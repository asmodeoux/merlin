"""``/revoke`` — admin command for invalidating an un-redeemed invite code.

Flow: admin types ``/revoke ABC123`` → bot checks the admin allowlist →
calls ``POST /invites/revoke`` on the API → confirms the revocation in
the chat. Non-admins get the same generic "command not found" reply
the rest of the operator surface uses, so the existence of the
revoke path stays hidden from public callers.

Use case is intentionally narrow: an admin who pasted a code into the
wrong DM, posted one to a public channel by mistake, or otherwise
needs to retire a code before it's redeemed. Already-redeemed codes
can't be revoked (the redemption already happened) — the route 404s
and we surface that as "this code is already consumed."
"""

from __future__ import annotations

import logging

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.admin.revoke")


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/revoke <code>`` — kill an un-redeemed invite code."""
    if update.effective_user is None or update.message is None:
        return

    admin_chat_ids: frozenset[int] = context.bot_data.get(
        BOT_DATA_ADMIN_KEY,
        frozenset(),
    )
    if update.effective_user.id not in admin_chat_ids:
        await update.message.reply_text(_unknown_command_text())
        return

    # context.args is the tail of the command line after `/revoke `.
    # PTB splits on whitespace; we accept exactly one token to keep the
    # surface tight (multi-code revoke can land later if there's demand).
    args = context.args or []
    if len(args) != 1 or not args[0].strip():
        await update.message.reply_text(_usage_text())
        return

    code = args[0].strip()
    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.revoke_invite(
            code=code,
            tg_user_id=update.effective_user.id,
        )
    except MerlinHTTPError as e:
        if e.status_code == 404:
            await update.message.reply_text(_already_consumed_text())
            return
        _log.warning("revoke_invite failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    revoked_at = result.get("revoked_at", "")
    await update.message.reply_text(
        _revoked_text(code=code, revoked_at=revoked_at),
        parse_mode="MarkdownV2",
    )


def build_handler() -> CommandHandler:
    """Wire the ``/revoke`` command handler."""
    return CommandHandler("revoke", revoke_command)


# ---------------------------------------------------------------------------
# Bot-voice strings.
# ---------------------------------------------------------------------------


def _revoked_text(*, code: str, revoked_at: str) -> str:
    """Success reply — confirms the code is dead.

    MarkdownV2-escaped code block matches ``/invite``'s output shape so
    the operator surface reads consistently across commands.
    """
    safe_code = _md2_escape(code)
    safe_time = _md2_escape(revoked_at)
    return f"Invite revoked\\.\n\n`{safe_code}`\n\nKilled at {safe_time}\\."


def _usage_text() -> str:
    return "Usage: /revoke <code>"


def _already_consumed_text() -> str:
    """The code is unknown, already redeemed, or already revoked.

    We collapse those three cases into one message because none of
    them are actionable for the admin: there's nothing to revoke.
    """
    return "That code can't be revoked — it's already consumed, revoked, or doesn't exist."


def _temporary_error_text() -> str:
    return "Couldn't revoke that code right now. Try again in a moment."


def _unknown_command_text() -> str:
    """Reply to a non-admin who types ``/revoke``.

    Same shape as ``/invite``'s non-admin response so the operator
    surface degrades to a uniform "I don't know that command" line.
    """
    return "I don't know that command. Try /help."


def _md2_escape(text: str) -> str:
    """Escape MarkdownV2 reserved characters.

    Mirrors the helper in ``invite_command.py`` so the two admin
    commands render their code blocks identically. Lifted instead of
    imported to keep the two handlers structurally independent.
    """
    reserved = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in reserved else c for c in text)
