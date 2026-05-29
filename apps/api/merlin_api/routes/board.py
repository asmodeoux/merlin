"""``/board`` — leaderboard."""

from __future__ import annotations

from fastapi import APIRouter, Query
from merlin_core.contracts import BoardResponse

router = APIRouter(tags=["board"])


@router.get(
    "/board",
    response_model=BoardResponse,
    summary="Leaderboard — top-N sources by accuracy over a window.",
    description=(
        "User-facing only: rank, display name, calls resolved, accuracy "
        "percent, last call date. No belief score, no simulated PnL, no "
        "version markers."
    ),
)
async def get_board(
    window: str = Query(default="all_time", pattern="^(all_time|30d|7d)$"),
) -> BoardResponse:
    """Return the leaderboard for the requested window. Sprint 1 wires the DB read."""
    return BoardResponse(window=window)
