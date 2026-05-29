"""``set_my_commands`` wiring — public + admin-scoped command lists.

Called from ``Application.post_init``. The admin scope uses
``BotCommandScopeChat(chat_id=admin_id)`` so admin commands only show
up in the admin chat's command picker; the default scope is what every
other user sees.

Idempotent — Telegram replaces the command list on every call. Safe to
run on every startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.error import BadRequest

from merlin_bot.config import Settings

if TYPE_CHECKING:
    from telegram.ext import Application

_log = logging.getLogger("merlin.bot.commands_setup")


# The public slash menu. Keep descriptions short — Telegram clips at
# ~256 chars per line and most clients render only the first ~40.
PUBLIC_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "Get started or redeem your invite"),
    BotCommand("alpha", "Log a new market call"),
    BotCommand("me", "Your open calls and hit rate"),
    BotCommand("board", "The leaderboard"),
    BotCommand("help", "How Merlin works"),
    BotCommand("cancel", "Drop the current draft"),
)


# Admin-only additions — appended to PUBLIC_COMMANDS for the admin's
# chat scope so admins see the full list, public users see the trimmed
# list.
ADMIN_EXTRA_COMMANDS: tuple[BotCommand, ...] = (BotCommand("invite", "Issue a new invite code"),)


async def register_commands(
    application: Application,
    *,
    settings: Settings | None = None,
) -> None:
    """Push the slash-command menu to Telegram for every relevant scope.

    Runs as part of ``Application.post_init`` so it fires once on every
    bot startup. Telegram replaces the menu on each call, so this is
    safe to re-run on every restart.

    ``settings`` is injectable for testing; if omitted, a fresh
    ``Settings()`` is constructed from the env.
    """
    if settings is None:
        settings = Settings()

    await application.bot.set_my_commands(
        commands=list(PUBLIC_COMMANDS),
        scope=BotCommandScopeDefault(),
    )
    _log.info("registered %d public commands", len(PUBLIC_COMMANDS))

    if not settings.telegram_admin_chat_ids:
        return

    admin_commands = list(PUBLIC_COMMANDS) + list(ADMIN_EXTRA_COMMANDS)
    for admin_chat_id in settings.telegram_admin_chat_ids:
        try:
            await application.bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_chat_id),
            )
        except BadRequest as e:
            # Telegram requires the chat to exist server-side before it
            # accepts a ``BotCommandScopeChat`` push — and the chat only
            # exists once the admin has sent the bot at least one
            # message. On a fresh deploy this means the first startup
            # logs ``Chat not found`` for every configured admin; the
            # next startup (after the admin runs ``/start``) succeeds.
            # We log a warning instead of crashing the bot because the
            # public command menu has already been pushed successfully
            # and the bot is otherwise fully functional.
            _log.warning(
                "could not push admin commands to chat_id=%s (%s). "
                "If you haven't /start'd the bot yet from this account, "
                "do so once and restart the bot; the admin menu will "
                "appear on the next push.",
                admin_chat_id,
                e,
            )
            continue
        _log.info(
            "registered %d admin commands for chat_id=%s",
            len(admin_commands),
            admin_chat_id,
        )
