"""Invite-related wire schemas (``POST /invites/redeem``, ``POST /invites``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RedeemInviteRequest(BaseModel):
    """Body of ``POST /invites/redeem``.

    Bot sends this after the user pastes their code.
    """

    code: str = Field(..., min_length=1, max_length=64)
    tg_user_id: int
    tg_handle: str | None = None


class RedeemInviteResponse(BaseModel):
    """Response: opaque ``source_id`` + next-step hint.

    ``source_id`` is the opaque ``'tg:<sha256>'`` handle from
    ``merlin_core.domain.source_id.make_source_id`` — a string, never a
    UUID (MVP §15 invariant 9). The previous draft of this contract
    typed it as ``UUID``; that was wrong and the stub redeem endpoint
    only "worked" because it returned a random UUID the bot never
    looked at.

    ``display_name_prompt`` is True iff the source row was just created
    and the bot should ask the user for a leaderboard name. False on
    idempotent re-redeem (same Telegram user pasting the same code
    again — we return their existing source_id).
    """

    source_id: str = Field(..., min_length=4, max_length=64)
    display_name_prompt: bool = True


class CreateInviteRequest(BaseModel):
    """Body of ``POST /invites`` — admin issues a new code.

    Admin endpoints don't need ``created_by_source_id`` in the body;
    the route resolves it server-side from the calling admin's TG id.
    Empty body is allowed (the route defaults ``expires_in_days``).
    """

    expires_in_days: int = Field(default=14, ge=1, le=365)
    tg_user_id: int = Field(
        ...,
        description=(
            "Telegram ID of the admin issuing the invite. The route "
            "checks this against the API's admin allowlist and resolves "
            "it to the admin's opaque source_id."
        ),
    )


class CreateInviteResponse(BaseModel):
    """Response: the freshly minted code, ready to paste."""

    code: str = Field(..., min_length=8, max_length=64)
    expires_at: datetime


class RevokeInviteRequest(BaseModel):
    """Body of ``POST /invites/revoke`` — admin invalidates an un-redeemed code.

    Admin-only. The route checks ``tg_user_id`` against the API's
    admin allowlist and 403s non-admins. ``code`` is the exact value
    the bot returned from ``POST /invites``.
    """

    code: str = Field(..., min_length=1, max_length=64)
    tg_user_id: int = Field(
        ...,
        description=(
            "Telegram ID of the admin revoking the invite. Checked "
            "against the API's admin allowlist."
        ),
    )


class RevokeInviteResponse(BaseModel):
    """Response: the code that was revoked + the timestamp it was killed.

    Returning the code back lets the bot echo the revocation
    confirmation without remembering what the admin typed. ``revoked_at``
    is the server's wall-clock time at the moment of the UPDATE so the
    admin sees the actual recorded value, not an approximation.
    """

    code: str = Field(..., min_length=1, max_length=64)
    revoked_at: datetime
