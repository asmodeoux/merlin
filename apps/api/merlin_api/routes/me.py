"""``/me`` routes — reputation snapshot + display-name PATCH."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from merlin_core.contracts import MeResponse, UpdateMeRequest

from merlin_api.deps import get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["sources"])


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Current source's open + resolved calls and headline stats.",
    description=(
        "User-visible only: open calls (asset + direction + resolve date), "
        "recent resolved calls with plain-percent returns, accuracy. "
        "No internal scoring fields, no version markers."
    ),
)
async def get_me() -> MeResponse:
    """Return the requesting source's snapshot. Sprint 1 wires the DB read."""
    return MeResponse()


@router.patch(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set the display name shown on the leaderboard.",
    description=(
        "Idempotent — calling with the same name is a no-op. The display "
        "name has a 40-character soft cap; the bot trims user input before "
        "sending. 404s if the caller's ``tg_user_id`` has no source row yet."
    ),
)
async def update_me(
    body: UpdateMeRequest,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Persist the display name against the calling source's row."""
    from merlin_infrastructure.db.repositories import (
        SourceIdentityRepository,
        SourceRepository,
    )

    identity_repo = SourceIdentityRepository(session)
    source_repo = SourceRepository(session)

    source_id = await identity_repo.get_source_id_by_tg_user_id(body.tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    await source_repo.update_display_name(source_id, body.display_name)
    await session.commit()
