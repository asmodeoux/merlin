"""``/me`` routes — reputation snapshot + display-name PATCH."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, status
from merlin_core.contracts import MeResponse, UpdateMeRequest
from merlin_core.contracts.board import OpenCallEntry

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
        "``tg_user_id`` query param resolves the caller via the "
        "``source_identity`` table (same convention as ``PATCH /me``). "
        "404s if the caller hasn't been onboarded yet."
    ),
)
async def get_me(
    tg_user_id: int = Query(
        ...,
        description=(
            "Caller's raw Telegram user id. The HMAC bearer "
            "authenticates the service, not the per-user identity, so "
            "the API needs the TG id on every authenticated call (we "
            "have no per-user JWT at v0). Same convention as "
            "``PATCH /me``."
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Return the requesting source's snapshot.

    Honest empty-state policy: no resolved calls yet → ``accuracy_pct``
    is ``None`` (not 0.0) and ``rank`` is ``None`` (not 0). The bot
    renders these as "tracking your first resolutions" rather than a
    misleading zero percent — see ``MeResponse`` docstring.
    """
    from merlin_infrastructure.db.repositories import (
        ClaimRepository,
        SourceIdentityRepository,
        SourceRepository,
        SourceReputationRepository,
    )

    identity_repo = SourceIdentityRepository(session)
    source_repo = SourceRepository(session)
    claim_repo = ClaimRepository(session)
    reputation_repo = SourceReputationRepository(session)

    source_id = await identity_repo.get_source_id_by_tg_user_id(tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    source = await source_repo.get_by_source_id(source_id)
    open_claims = await claim_repo.list_open_by_source(source_id)
    # ``count_resolved_by_source`` proxies "claims whose horizon has
    # passed" — kept as the fallback so a user mid-recompute (the
    # worker hasn't INSERTed the reputation snapshot yet) still sees
    # an honest "you have N past-horizon calls" rather than a zero.
    resolved_total_proxy = await claim_repo.count_resolved_by_source(source_id)
    # ``source_reputation_history`` row is the canonical answer when
    # it exists. Cold-start sources return ``None`` and the bot
    # renders the "tracking your first resolutions" empty-state.
    reputation = await reputation_repo.latest_for_source(source_id)

    calls_resolved_total = (
        reputation.outcomes_resolved if reputation is not None else resolved_total_proxy
    )
    accuracy_pct = reputation.accuracy_pct if reputation is not None else None
    weighted_pnl_pct = reputation.weighted_pnl_pct if reputation is not None else None

    return MeResponse(
        display_name=source.display_name if source is not None else None,
        calls_open=[
            OpenCallEntry(
                claim_id=row.id,
                asset_symbol=row.asset_symbol,
                direction=row.direction,
                horizon_seconds=row.horizon_seconds,
                resolves_at=row.resolves_at,
                # NULL on legacy rows (predating the column) and on
                # rows where CoinGecko was down at submit time. The
                # bot renders the entry-price line iff this is set.
                entry_price_usd=row.price_usd_at_submit,
            )
            for row in open_claims
        ],
        calls_resolved_recent=[],
        calls_resolved_total=calls_resolved_total,
        accuracy_pct=accuracy_pct,
        weighted_pnl_pct=weighted_pnl_pct,
        # ``rank`` would require a cross-source query (the user's
        # row's position in the ordered ``source_reputation_history``
        # leaderboard). Deferred — the bot doesn't render the value
        # on /me today, and surfacing it without a clean projection
        # would couple this route to the full leaderboard query.
        rank=None,
    )


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
