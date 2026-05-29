"""``/me`` routes — reputation snapshot + display-name PATCH."""

from __future__ import annotations

from fastapi import APIRouter, status
from merlin_core.contracts import MeResponse, UpdateMeRequest

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
        "sending."
    ),
)
async def update_me(body: UpdateMeRequest) -> None:
    """Persist the display name. Sprint 1 wires this against ``sources``."""
    # The body validation alone is the v0 contract; the persist call
    # lives in the use case (Sprint 1 wiring).
    _ = body
