"""``/me`` routes — reputation snapshot + display-name PATCH + per-ticker / history breakdowns."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, status
from merlin_core.application.use_cases.tickers import compute_ticker_breakdown
from merlin_core.contracts import (
    HistoryEntry,
    HistoryResponse,
    MeResponse,
    TickerEntry,
    TickersResponse,
    UpdateMeRequest,
)
from merlin_core.contracts.board import OpenCallEntry
from merlin_core.contracts.history import (
    HISTORY_DEFAULT_PAGE_SIZE,
    HISTORY_MAX_PAGE_SIZE,
)
from merlin_core.domain.scoring import is_flat

from merlin_api.deps import Ports, get_ports, get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("merlin.api.routes.me")

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
        "sending. 404s if the caller's ``tg_user_id`` has no source row yet. "
        "On the first-ever display-name set for an invited user, sends the "
        "inviter a notification — fired exactly once per invitee, gated on "
        "the previous ``display_name`` being NULL so a later rename does "
        "not re-notify."
    ),
)
async def update_me(
    body: UpdateMeRequest,
    session: AsyncSession = Depends(get_session),
    ports: Ports = Depends(get_ports),
) -> None:
    """Persist the display name and notify the inviter on first-time set."""
    from merlin_infrastructure.db.repositories import (
        InviteRepository,
        SourceIdentityRepository,
        SourceRepository,
    )
    from merlin_infrastructure.notify.telegram.templates import invitee_joined

    identity_repo = SourceIdentityRepository(session)
    source_repo = SourceRepository(session)
    invite_repo = InviteRepository(session)

    source_id = await identity_repo.get_source_id_by_tg_user_id(body.tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    # Snapshot the previous display_name BEFORE the update so we can
    # tell "first-time set" (None → some name) apart from a later
    # rename. We only notify on the first-time transition — calling
    # PATCH /me again to change a name MUST NOT re-fire the
    # handshake message to the inviter.
    existing = await source_repo.get_by_source_id(source_id)
    is_first_time_name_set = existing is not None and existing.display_name is None

    await source_repo.update_display_name(source_id, body.display_name)
    await session.commit()

    if is_first_time_name_set:
        inviter_tg_user_id: int | None = None
        try:
            inviter_tg_user_id = await invite_repo.get_inviter_tg_user_id_by_redeemed_source_id(
                source_id,
            )
        except Exception:
            # Lookup failure must never break the user's onboarding —
            # the row is already committed, the user already sees
            # their new name. We just lose this one handshake ping.
            _log.exception(
                "update_me: inviter lookup failed for source %s — "
                "user is onboarded, inviter notification skipped",
                source_id,
            )

        if inviter_tg_user_id is not None:
            try:
                await ports.notifier.notify_user(
                    inviter_tg_user_id,
                    invitee_joined(name=body.display_name),
                )
            except Exception:
                _log.exception(
                    "update_me: inviter notify_user failed for source %s — "
                    "user is onboarded, inviter ping was lost",
                    source_id,
                )


@router.get(
    "/me/tickers",
    response_model=TickersResponse,
    summary="Per-ticker breakdown of the caller's resolved calls.",
    description=(
        "User-facing only: ticker, conviction-weighted PnL %, hit rate %, "
        "resolved-call count. Sorted server-side by the same Bayesian-"
        "shrunk score the leaderboard uses, so single-bet tickers with "
        "outsized PnL never out-rank consistent multi-bet tickers. "
        "Cold-start tickers (no resolved calls) are filtered out — every "
        "row has at least one resolved bet. 404s if the caller has no "
        "source row yet."
    ),
)
async def get_my_tickers(
    tg_user_id: int = Query(
        ...,
        description=(
            "Caller's raw Telegram user id. Same identity-lookup "
            "convention as ``GET /me`` / ``PATCH /me``."
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> TickersResponse:
    """Return the per-ticker scoring breakdown for the caller's portfolio."""
    from merlin_infrastructure.db.repositories import (
        OutcomeRepository,
        SourceIdentityRepository,
    )

    identity_repo = SourceIdentityRepository(session)
    outcome_repo = OutcomeRepository(session)

    source_id = await identity_repo.get_source_id_by_tg_user_id(tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    rows = await outcome_repo.fetch_resolved_for_tickers(source_id)
    stats = compute_ticker_breakdown(rows)
    return TickersResponse(
        entries=[
            TickerEntry(
                asset_symbol=s.asset_symbol,
                calls_resolved=s.calls_resolved,
                calls_correct=s.calls_correct,
                accuracy_pct=s.accuracy_pct,
                weighted_pnl_pct=s.weighted_pnl_pct,
            )
            for s in stats
        ],
    )


@router.get(
    "/me/history",
    response_model=HistoryResponse,
    summary="The caller's resolved calls, newest first.",
    description=(
        "Paginated via ``limit`` / ``offset`` with a frozen ``as_of`` "
        "window ceiling. Default page size is 30, matching the bot's "
        '"Show 30 more" inline button. The first page omits ``as_of`` '
        "and the server stamps it with the request's wall-clock UTC "
        "time; the response echoes the value back, and the bot must "
        "round-trip it on subsequent pages so a new outcome landing "
        "mid-session can't shift already-paged rows down into the "
        "next page (which would dup the boundary row on the next "
        "click). The response also carries ``has_more`` + "
        "``next_offset`` so the bot can render the button without "
        "keeping its own count. 404s if the caller has no source row yet."
    ),
)
async def get_my_history(
    tg_user_id: int = Query(
        ...,
        description=(
            "Caller's raw Telegram user id. Same identity-lookup convention as ``GET /me``."
        ),
    ),
    limit: int = Query(
        default=HISTORY_DEFAULT_PAGE_SIZE,
        ge=1,
        le=HISTORY_MAX_PAGE_SIZE,
        description=(
            "Maximum rows to return in this page. Defaults to 30 — one "
            "bot page. Capped at 100 so a misbehaving client can't drain "
            "the database in one request."
        ),
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description=(
            "Zero-based index of the first row to return inside the "
            "``as_of``-frozen window. The bot encodes (as_of, offset) "
            "in the inline-button callback_data and round-trips both."
        ),
    ),
    as_of: datetime | None = Query(
        default=None,
        description=(
            "Frozen window ceiling — only outcomes that resolved at or "
            "before this timestamp are returned. The first page omits "
            "this; the server stamps it with the current UTC time and "
            "echoes it back. Subsequent pages must round-trip the "
            "same value to avoid pagination drift when a new outcome "
            "resolves mid-session."
        ),
    ),
    session: AsyncSession = Depends(get_session),
) -> HistoryResponse:
    """Return the caller's resolved-call history, newest first, paginated."""
    from merlin_infrastructure.db.repositories import (
        OutcomeRepository,
        SourceIdentityRepository,
    )

    identity_repo = SourceIdentityRepository(session)
    outcome_repo = OutcomeRepository(session)

    source_id = await identity_repo.get_source_id_by_tg_user_id(tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    # Freeze the window on the first request and echo it back so the
    # bot can round-trip it on follow-up pages. We always normalise to
    # UTC so the comparison ``resolved_at <= as_of`` is done in the
    # same timezone the DB stores (timestamps are TIMESTAMP WITH TIME
    # ZONE; SQLAlchemy + asyncpg handle the conversion either way, but
    # we keep the wire shape explicit-UTC).
    window_as_of = _normalise_as_of(as_of)

    # Single SELECT returns ``(rows, total)`` via a window function —
    # one round-trip, count guaranteed consistent with the page. The
    # supersedes-chain filter (MVP §15 invariant 6) lives in the
    # repository's SQL so we never count or show a superseded row.
    rows, total = await outcome_repo.fetch_history_page(
        source_id,
        as_of=window_as_of,
        limit=limit,
        offset=offset,
    )
    end_of_page = offset + len(rows)
    has_more = end_of_page < total
    next_offset = end_of_page if has_more else None

    return HistoryResponse(
        entries=[
            HistoryEntry(
                claim_id=row.claim_id,
                asset_symbol=row.asset_symbol,
                direction=row.direction,
                horizon_seconds=row.horizon_seconds,
                observed_return_pct=row.observed_return_pct,
                is_correct=row.is_correct,
                # ``is_flat`` is computed server-side so the bot can
                # pick its won/flat/lost emoji without importing
                # ``merlin_core.domain.scoring`` (forbidden under the
                # bot layer rule). Same threshold as the live
                # resolution-notification dispatch by construction.
                is_flat=is_flat(row.observed_return_pct),
                resolved_at=row.resolved_at,
            )
            for row in rows
        ],
        has_more=has_more,
        next_offset=next_offset,
        as_of=window_as_of,
    )


def _normalise_as_of(value: datetime | None) -> datetime:
    """Pick a UTC, second-truncated ``as_of`` for the history window.

    First page (``None``) → ``datetime.now(UTC)``. Subsequent pages
    pass back the value the server returned on page 1; we coerce
    timezone-naive inputs to UTC (Pydantic parses a naive
    ``2026-06-12T14:30:00`` as naive) and convert any other offset
    to UTC so the SQL filter ``resolved_at <= :as_of`` works against
    the storage tzinfo cleanly.

    Why truncate to second precision: the bot's "Show 30 more" inline
    button encodes ``as_of`` in its ``callback_data`` as an integer
    Unix epoch (callback_data caps at 64 bytes, so seconds keeps the
    payload short). If the server stamped a microsecond-precise
    ``as_of`` on page 1 but the bot rounded it down on page 2, an
    outcome resolving in the sub-second window between the two would
    pass the page-1 filter (``resolved_at <= 12:34:56.789``) but fail
    the page-2 filter (``resolved_at <= 12:34:56.000``) — dropping
    one row and shifting every subsequent row up. Truncating
    server-side aligns both ends.

    Trade-off: an outcome that resolves in the same second as the
    user types ``/history`` may not appear until the next refresh.
    Acceptable — the user's expectation of freshness is fuzzy, and
    the alternative (no truncation) silently corrupts pagination.
    """
    if value is None:
        normalised = datetime.now(UTC)
    elif value.tzinfo is None:
        normalised = value.replace(tzinfo=UTC)
    else:
        normalised = value.astimezone(UTC)
    return normalised.replace(microsecond=0)
