"""Invite-related wire schemas (``POST /invites/redeem``)."""

from __future__ import annotations

from uuid import UUID

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

    ``display_name_prompt`` is True iff the source row was just created
    and the bot should ask the user for a leaderboard name.
    """

    source_id: UUID
    display_name_prompt: bool = True
