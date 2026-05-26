"""set_my_commands wiring — public + admin-scoped command lists.

Called from `Application.post_init`. The admin scope uses
`BotCommandScopeChat(chat_id=admin_id)` so admin commands only show up
in the admin chat's command picker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application


async def register_commands(_application: Application) -> None:
    """Register PTB commands for the public + admin scopes.

    Sprint 1: call `application.bot.set_my_commands(...)` twice — once for
    the public scope, once per admin chat via `BotCommandScopeChat`.
    """
