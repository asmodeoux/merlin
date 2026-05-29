"""``/board`` — leaderboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query
from merlin_core.contracts import BoardEntry, BoardResponse

from merlin_api.deps import get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["board"])


@router.get(
    "/board",
    response_model=BoardResponse,
    summary="Leaderboard — top-N sources by accuracy over a window.",
    description=(
        "User-facing only: rank, display name, calls resolved, accuracy "
        "percent, last call date. No belief score, no simulated PnL, no "
        "version markers. Returns an empty list until the outcomes "
        "layer ships — the bot renders an honest empty-state then."
    ),
)
async def get_board(
    window: str = Query(default="all_time", pattern="^(all_time|30d|7d)$"),
    session: AsyncSession = Depends(get_session),
) -> BoardResponse:
    """Return the leaderboard for the requested window.

    Today the underlying ``ClaimRepository.leaderboard(window)`` is
    empty (no outcomes table yet). The route is wired through anyway
    so the swap-in is a one-line change in the repository when the
    worker lands — bot, contract, and route stay put.
    """
    from merlin_infrastructure.db.repositories import ClaimRepository

    repo = ClaimRepository(session)
    rows = await repo.leaderboard(window)
    return BoardResponse(
        window=window,
        entries=[
            BoardEntry(
                rank=row.rank,
                display_name=row.display_name,
                calls_resolved=row.calls_resolved,
                calls_correct=row.calls_correct,
                accuracy_pct=row.accuracy_pct,
                last_call_at=row.last_call_at,
            )
            for row in rows
        ],
    )
