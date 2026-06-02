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
    summary="Leaderboard — top-N sources ranked by conviction-weighted PnL.",
    description=(
        "User-facing only: rank, display name, calls resolved, "
        "accuracy %, conviction-weighted PnL %, last call date. No "
        "belief score, no simulated dollar PnL, no version markers. "
        "Sort order is internal (Bayesian-shrunk PnL); the bot renders "
        "PnL as the primary headline with hit rate as a secondary number."
    ),
)
async def get_board(
    window: str = Query(default="all_time", pattern="^(all_time|30d|7d)$"),
    session: AsyncSession = Depends(get_session),
) -> BoardResponse:
    """Return the leaderboard for the requested window.

    Thin pass-through over ``SourceReputationRepository.leaderboard``,
    which already filters cold-start sources and orders by the
    Bayesian-shrunk sort score. The route maps the repository's
    ``LeaderboardEntry`` records to ``BoardEntry`` wire DTOs 1:1.
    """
    from merlin_infrastructure.db.repositories import SourceReputationRepository

    repo = SourceReputationRepository(session)
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
                weighted_pnl_pct=row.weighted_pnl_pct,
                last_call_at=row.last_call_at,
            )
            for row in rows
        ],
    )
