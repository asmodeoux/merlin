"""Source-management wire schemas.

Two endpoints land in Sprint 1:

* ``POST /sources/admin_bootstrap`` — an admin's very first ``/start``
  creates their ``sources`` row without an invite code. The endpoint
  validates the calling Telegram ID against the API's admin allowlist
  (``Settings.telegram_admin_chat_ids``) and is idempotent: a second
  call from the same admin returns their existing ``source_id``
  rather than creating a duplicate.

* ``PATCH /me`` — already shipped (``me.py``); leaderboard display name.

Future endpoints (``GET /sources/{id}``, admin source-graph reads)
extend this module.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminBootstrapRequest(BaseModel):
    """Body of ``POST /sources/admin_bootstrap``.

    Bot sends the calling Telegram user's id + handle. The route
    validates the id is in ``Settings.telegram_admin_chat_ids`` and
    400s otherwise (admins-only).
    """

    tg_user_id: int
    tg_handle: str | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=40)


class AdminBootstrapResponse(BaseModel):
    """Response: opaque ``source_id`` + display-name prompt hint.

    Mirrors ``RedeemInviteResponse`` so the bot's caller can reuse its
    onboarding state machine. ``display_name_prompt`` is True iff a
    brand-new admin row was created without a display name set.
    """

    source_id: str = Field(..., min_length=4, max_length=64)
    display_name_prompt: bool = True
