"""Admin-only bot handlers.

Operator surface — only enabled for Telegram users whose chat IDs are
in ``Settings.telegram_admin_chat_ids``. Every handler in this package
checks the admin allowlist inline before doing any work; mis-targeted
admin commands degrade to a generic "unknown command" reply rather
than leaking the existence of the operator surface.

Currently houses ``/revoke`` only. ``/invite`` used to live here as
an admin command; it moved to the public handlers package
(``merlin_bot.handlers.invite_command``) once invite issuance was
opened up to every onboarded source — the API now gates issuance on
"caller is a known source", not "caller is in the admin allowlist".
"""

from merlin_bot.handlers.admin.revoke_command import build_handler as build_revoke_handler

__all__ = ["build_revoke_handler"]
