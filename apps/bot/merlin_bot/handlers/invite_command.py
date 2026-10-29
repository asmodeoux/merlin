"""``/invite`` — public command for issuing fresh invite codes.

Flow: any onboarded user types ``/invite`` → bot calls
``POST /invites`` on the API → replies with the freshly minted code +
expiry. The API enforces the only gate: the caller must already have
a ``sources`` row (i.e. they've finished ``/start``). Pre-onboarding
callers hit a friendly "send /start first" nudge from the 403 path.

No admin allowlist. No quota at v0 — every onboarded source can mint
unlimited codes. Sybil-resistance lives entirely in the chain-of-trust
property (you have to be invited to mint invites). A per-source quota
is the follow-up if the chain alone proves insufficient.

The code returned is the only thing the user shares with the
invitee — pasting it into the bot from a different Telegram account
hits ``POST /invites/redeem`` and onboards them.
"""

from __future__ import annotations

import logging

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.formatting import format_utc_datetime

_log = logging.getLogger("merlin.bot.handlers.invite")


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/invite`` — mint a fresh invite code and reply with it."""
    if update.effective_user is None or update.message is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.create_invite(
            tg_user_id=update.effective_user.id,
            expires_in_days=14,
        )
    except MerlinHTTPError as e:
        # 403 from the API means "caller is not a sources row" — i.e.
        # they typed /invite before completing /start. Route this to a
        # friendly "join first" nudge so the user understands the
        # remediation. Every other error shape is a real outage from
        # the user's POV — recoverable with retry.
        if e.status_code == 403:
            await update.message.reply_text(_not_onboarded_text())
            return
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
    user needs to copy out. The expiry timestamp is rendered through
    ``format_utc_datetime`` (``"June 22 @ 02:17 UTC"``) so we keep
    the exact moment the code dies but in a human-readable shape —
    matching the /alpha receipt + /me open-call lines per AGENTS.md's
    "no ISO timestamps in user-visible text" rule.
    """
    safe_expiry = _md2_escape(format_utc_datetime(expires_at))
    safe_code = _md2_escape(code)
    return (
        f"Here's a fresh invite to share\\.\n\n"
        f"`{safe_code}`\n\n"
        f"Good for 14 days, one use only\\. Expires {safe_expiry}\\."
    )


def _not_onboarded_text() -> str:
    """Reply when the API 403s because the caller has no ``sources`` row.

    Realistic paths to this state: the user typed ``/invite`` before
    completing ``/start``, or a partial onboarding (e.g. /start
    crashed mid-flow) left them without a source row despite Telegram
    knowing them. Either way, the remediation is the same.
    """
    return "Send /start first to join — then you can share invites with anyone."


def _temporary_error_text() -> str:
    """Recoverable API error; ask the user to try again shortly."""
    return "Couldn't mint a code right now. Try again in a moment."


def _md2_escape(text: str) -> str:
    """Escape MarkdownV2 reserved characters.

    Telegram's MarkdownV2 reserves these for formatting:
    ``_ * [ ] ( ) ~ ` > # + - = | { } . !``
    Anywhere they appear in literal content (including the code body
    or the timestamp), they have to be backslash-prefixed.
    """
    reserved = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in reserved else c for c in text)
