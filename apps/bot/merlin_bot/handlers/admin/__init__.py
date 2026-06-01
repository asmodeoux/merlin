"""Admin-only bot handlers.

Operator surface — only enabled for Telegram users whose chat IDs are
in ``Settings.telegram_admin_chat_ids``. Every handler in this package
checks the admin allowlist inline before doing any work; mis-targeted
admin commands degrade to a generic "unknown command" reply rather
than leaking the existence of the operator surface.
"""

from merlin_bot.handlers.admin.invite_command import build_handler as build_invite_handler
from merlin_bot.handlers.admin.revoke_command import build_handler as build_revoke_handler

__all__ = ["build_invite_handler", "build_revoke_handler"]
