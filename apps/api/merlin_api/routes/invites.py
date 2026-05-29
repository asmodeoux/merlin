"""Invite routes — ``POST /invites/redeem``."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from merlin_core.contracts import RedeemInviteRequest, RedeemInviteResponse

router = APIRouter(tags=["invites"])


@router.post(
    "/invites/redeem",
    response_model=RedeemInviteResponse,
    status_code=status.HTTP_200_OK,
    summary="Redeem an invite code (atomic — INSERT sources + UPDATE invites).",
    description=(
        "Single transaction: validate invite, INSERT ``sources`` "
        "(zeroed Admiralty: ``reliability='F'``, ``credibility=6``), "
        "INSERT ``source_identity``, UPDATE ``invites`` redeemed. "
        "Returns the new ``source_id`` and a hint to prompt for "
        "display name. Unknown / expired codes 404."
    ),
)
async def redeem_invite(body: RedeemInviteRequest) -> RedeemInviteResponse:
    """Look up the invite and create the source row atomically."""
    # Sprint 1 wires this against the DB. The contract is locked here
    # (request + response shape) so the bot can be implemented in
    # parallel against the wire.
    if not body.code or body.code.lower() in {"invalid", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invite code not found or already redeemed",
        )
    return RedeemInviteResponse(source_id=uuid4(), display_name_prompt=True)
