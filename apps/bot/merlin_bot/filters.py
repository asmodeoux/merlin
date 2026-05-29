"""Custom PTB filters — IsAdmin, IsOnboarded.

Composable with PTB's built-in `filters` via `&`/`|`. Sprint 1 wires
real checks (admin chat IDs from Settings; onboarding state from
`MerlinClient`, cached on `user_data`).
"""

from __future__ import annotations

from telegram.ext.filters import MessageFilter


class IsAdmin(MessageFilter):
    """Pass updates whose `effective_user.id` is in the admin allowlist."""

    def filter(self, message: object) -> bool:
        """Return True if the update's user is in the admin allowlist."""
        return False  # Sprint 1: check chat_id against settings.telegram_admin_chat_ids


class IsOnboarded(MessageFilter):
    """Pass updates from users who completed onboarding."""

    def filter(self, message: object) -> bool:
        """Return True if the update's user has completed onboarding."""
        return True  # Sprint 1: fetch onboarding state from MerlinClient.me()


is_admin = IsAdmin()
is_onboarded = IsOnboarded()
