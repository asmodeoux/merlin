"""``/board`` — leaderboard."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query
from merlin_core.contracts import BoardEntry, BoardResponse
from merlin_core.contracts.board import HorizonFilter

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
    horizon: str = Query(
        default="all",
        pattern="^(all|day|swing|position)$",
        description=(
            "Four-mode horizon filter the bot surfaces as 'Day Traders' "
            "(<=24h), 'Swing Traders' (1d-3wk), 'Position Traders' (>3wk), "
            "and 'All'. Boundaries are HORIZON_DAY_TRADER_MAX_SECONDS and "
            "HORIZON_SWING_TRADER_MAX_SECONDS in merlin_core.contracts.horizons. "
            "'all' reads the pre-aggregated snapshot table; 'day' / 'swing' / "
            "'position' recompute on-the-fly so the snapshot table stays "
            "horizon-agnostic."
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> BoardResponse:
    """Return the leaderboard for the requested window + horizon filter.

    Thin pass-through over ``SourceReputationRepository.leaderboard``,
    which already filters cold-start / empty-slice sources and orders
    by the Bayesian-shrunk sort score. The route maps the repository's
    ``LeaderboardEntry`` records to ``BoardEntry`` wire DTOs 1:1 and
    echoes ``horizon`` back so the bot can render the active filter
    in the message header.
    """
    from merlin_infrastructure.db.repositories import SourceReputationRepository

    repo = SourceReputationRepository(session)
    rows = await repo.leaderboard(window, horizon=horizon)
    # FastAPI's regex ``pattern`` narrows ``horizon`` at runtime to
    # the three valid literals; mypy can't see that constraint, so
    # the cast pins the type at the wire boundary.
    return BoardResponse(
        window=window,
        horizon=cast(HorizonFilter, horizon),
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
