"""Claim routes — draft (no DB) + commit (atomic) + inspect.

``POST /claims/draft`` is the freeform-LLM extraction endpoint (Q-028).
Stateless. No DB writes. One LLM call, optionally one CoinGecko
``/search`` call to populate candidates.

``POST /claims`` is the atomic commit. The use case
``merlin_core.application.use_cases.submit_claim`` runs five INSERTs +
one RQ ``enqueue_at`` inside a single transaction.

``GET /claims/{id}`` returns user-visible claim state — verbatim
thesis, resolves date, outcome if any. No internal scoring fields.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from merlin_core.application.ports import (
    AssetNotFoundError,
    ClaimDrafterError,
    MarketDataError,
)
from merlin_core.contracts import (
    AssetCandidateDTO,
    ClaimDraftRequest,
    ClaimDraftResponse,
    ClaimResponse,
    ExtractedFieldsDTO,
    ParseMetadataDTO,
    SubmitClaimRequest,
)
from merlin_core.contracts.horizons import horizon_to_timedelta
from merlin_core.domain import ClaimDraft

from merlin_api.deps import Ports, get_ports, get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("merlin.api.routes.claims")

router = APIRouter(tags=["claims"])

# The per-field confidence floor for ``asset_query`` lives on the
# drafter (``merlin_infrastructure.llm.openrouter._ASSET_CONFIDENCE_FLOOR``).
# The API trusts the drafter's gating and does not re-check the
# confidence here — keeping the floor in one place avoids skew
# (Sprint-1 bug had both layers gating independently AND the API
# dropping the drafter's value on the floor anyway).


@router.post(
    "/claims/draft",
    response_model=ClaimDraftResponse,
    status_code=status.HTTP_200_OK,
    summary="Freeform-LLM claim extraction (no DB writes).",
    description=(
        "Stateless. One LLM call, optionally one CoinGecko ``/search`` call. "
        "Returns a prefilled draft for the bot to render on its preview. "
        "On any LLM failure, returns 503 — the bot then falls back to the "
        "button form."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "LLM unavailable; bot should fall back to the button flow."
        },
    },
)
async def draft_claim(
    body: ClaimDraftRequest,
    ports: Ports = Depends(get_ports),
) -> ClaimDraftResponse:
    """Return a structured ``ClaimDraft`` for the user's freeform text."""
    try:
        draft = await ports.drafter.draft(body.text)
    except ClaimDrafterError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"claim drafter unavailable: {e}",
        ) from e

    candidates: list[AssetCandidateDTO] = []
    # The drafter is the single source of truth for the asset query: it
    # extracts the field, applies the per-field confidence floor, and
    # leaves ``asset_query`` set ONLY when the LLM was confident enough
    # for us to surface the candidates without a clarifying question.
    # If the floor culled it (or the user didn't mention an asset at
    # all), we leave ``asset_query`` None and the bot opens the asset
    # picker. We MUST NOT fall back to ``draft.thesis_text`` here —
    # the thesis is the verbatim full sentence, and feeding it to
    # CoinGecko ``/search`` returns either nothing or wildly wrong
    # tokens (Sprint-1 live bug: "ETH long 2 weeks, Vitalik announced
    # a large upgrade to staking" → zero candidates for an obvious ETH
    # call).
    asset_query: str | None = draft.asset_query
    if asset_query:
        try:
            candidates_domain = await ports.market_data.search_assets(asset_query)
            candidates = [
                AssetCandidateDTO(
                    coingecko_id=c.coingecko_id,
                    symbol=c.symbol,
                    name=c.name,
                    market_cap_rank=c.market_cap_rank,
                    current_price_usd=c.current_price_usd,
                    market_cap_usd=c.market_cap_usd,
                )
                for c in candidates_domain
            ]
        except MarketDataError:
            # Non-fatal: the bot will surface an asset-picker prompt
            # instead of pre-filled candidates.
            candidates = []

    return _to_draft_response(draft, asset_query, candidates)


@router.post(
    "/claims",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a claim (persist + schedule resolution).",
    description=(
        "Persists the ``claims`` row and enqueues the RQ resolution job "
        "inside one request. ``tg_user_id`` is resolved to a "
        "``source_id`` via the ``source_identity`` table; unknown users "
        "404 (onboard via ``/invites/redeem`` or "
        "``/sources/admin_bootstrap`` first). Returns the persisted "
        "claim with its resolve date."
    ),
)
async def submit_claim(
    body: SubmitClaimRequest,
    ports: Ports = Depends(get_ports),
    session: AsyncSession = Depends(get_session),
) -> ClaimResponse:
    """Persist the claim, schedule its resolution, return the row."""
    # Confirm the asset exists upstream — the bot may have a stale id
    # if a token was delisted between the search and the commit.
    try:
        metadata = await ports.market_data.get_asset_metadata(body.coingecko_id)
    except AssetNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown coingecko_id: {body.coingecko_id}",
        ) from e
    except MarketDataError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"market data lookup failed: {e}",
        ) from e

    # Resolve caller -> source_id via the PII table. The route refuses
    # to write a claim for an un-onboarded user — without a source row,
    # ``/me`` and ``/board`` would have nothing to link the claim back
    # to and the FK on ``claims.source_id`` would 500.
    from merlin_infrastructure.db.repositories import (
        ClaimRepository,
        SourceIdentityRepository,
    )

    identity_repo = SourceIdentityRepository(session)
    source_id = await identity_repo.get_source_id_by_tg_user_id(body.tg_user_id)
    if source_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source not found — onboard via /invites/redeem first",
        )

    submitted_at = datetime.now(UTC)
    # body.horizon_seconds is integer seconds in the bounded range
    # ``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]`` per the DTO's
    # ``Field(ge=..., le=...)``. We add the duration directly — no
    # calendar/DST math — upholding MVP §15 invariant 4 by
    # construction.
    resolves_at = submitted_at + horizon_to_timedelta(body.horizon_seconds)
    claim_id = uuid4()

    # Best-effort entry-price snapshot. The user needs the price they
    # were betting against to sanity-check the call ("did I get the
    # right asset?", "was the price already weird?") and to make the
    # eventual outcome meaningful. We capture it once, here, and the
    # column is canonical for the receipt + /me + downstream worker.
    #
    # Failure is non-fatal: market-data being down at submit time
    # must NOT block the bet (same principle as the LLM-failure
    # fallback — market data is UX acceleration, never a system
    # dependency). On failure we log and persist NULL; the bot's
    # renderer simply omits the entry-price line.
    price_usd_at_submit = await _snapshot_entry_price(
        ports=ports,
        coingecko_id=body.coingecko_id,
        submitted_at=submitted_at,
    )

    # INSERT the claim row BEFORE enqueueing. If the enqueue fails we
    # rollback the row; if the row insert fails we never enqueue. The
    # opposite order (enqueue first, then INSERT) lands the worker on
    # a phantom claim if the INSERT fails — exactly the latent bug
    # the previous "no DB" path masked.
    claim_repo = ClaimRepository(session)
    await claim_repo.create(
        claim_id=claim_id,
        source_id=source_id,
        asset_coingecko_id=body.coingecko_id,
        asset_symbol=metadata.symbol,
        asset_name=metadata.name,
        direction=body.direction,
        horizon_seconds=body.horizon_seconds,
        conviction=body.conviction,
        thesis_text=body.thesis_text,
        asset_query=body.asset_query,
        submitted_at=submitted_at,
        resolves_at=resolves_at,
        parsed_via=body.parse_metadata.parsed_via,
        price_usd_at_submit=price_usd_at_submit,
        # ``mode="json"`` serializes datetimes (notably
        # ``parse_metadata.extracted_at``) to ISO strings so the JSONB
        # column's default ``json.dumps`` doesn't choke. The plain
        # ``model_dump()`` leaves them as ``datetime`` objects, which
        # blow up at flush time with "Object of type datetime is not
        # JSON serializable" — a 500 on every real LLM-driven submit.
        extracted_fields=body.extracted_fields.model_dump(mode="json"),
        parse_metadata=body.parse_metadata.model_dump(mode="json"),
    )

    # RQ's ``validate_job_id`` rejects anything outside ``[A-Za-z0-9_-]``,
    # so the historical ``resolve:{uuid}`` shape blew up at runtime
    # (live Sprint-1 bug). Use a dash separator — same idempotency
    # semantics (one job per claim), but it actually round-trips RQ.
    ports.queue.enqueue_at(
        resolves_at,
        "merlin_worker.jobs.resolve_outcome",
        str(claim_id),
        job_id=f"resolve-{claim_id}",
    )

    await session.commit()

    return ClaimResponse(
        claim_id=claim_id,
        asset_symbol=metadata.symbol,
        direction=body.direction,
        horizon_seconds=body.horizon_seconds,
        conviction=body.conviction,
        thesis_text=body.thesis_text,
        submitted_at=submitted_at,
        resolves_at=resolves_at,
        price_usd_at_submit=price_usd_at_submit,
        extracted_fields=body.extracted_fields,
        parse_metadata=body.parse_metadata,
        outcome=None,
    )


@router.get(
    "/claims/{claim_id}",
    response_model=ClaimResponse,
    summary="Fetch a claim by id.",
    description=(
        "Returns the user-visible shape of a claim: verbatim thesis, "
        "resolve date, outcome if resolved. No internal scoring fields."
    ),
)
async def get_claim(
    claim_id: UUID,
    ports: Ports = Depends(get_ports),  # wired in Sprint 1
) -> ClaimResponse:
    """Look up a claim. Sprint 1 wires this against the DB."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="GET /claims/{id} is wired against the DB in Sprint 1.",
    )


async def _snapshot_entry_price(
    *,
    ports: Ports,
    coingecko_id: str,
    submitted_at: datetime,
) -> float | None:
    """Capture the entry price snapshot for a claim at submit time.

    Best-effort by design — a CoinGecko outage at submit time must
    NOT block the bet (the same UX-acceleration-not-system-dependency
    rule that governs the LLM fallback). On *any* failure we log and
    return ``None``; the column stays NULL and the bot's renderer
    omits the entry-price line.

    Uses ``MarketDataPort.get_price_at(submitted_at)`` rather than a
    new "spot price" port method because (a) the existing 5-minute
    bucket window is more than fine for ``now`` and (b) it keeps the
    adapter surface narrow — the worker's resolution path already
    uses the same call.

    We catch ``Exception`` broadly (not just ``MarketDataError``)
    because the adapter only wraps ``httpx.HTTPError`` and status
    codes. A 200-OK response with a malformed JSON body or an
    unexpected payload shape (CoinGecko has changed shapes before;
    a future migration could change it again) raises
    ``json.JSONDecodeError`` / ``KeyError`` / ``TypeError`` /
    ``ValueError`` directly. Letting any of those escape would 500
    the submit even though the bet itself is fine without a price.
    Treat every failure as "no snapshot" and let the bot's renderer
    omit the entry-price line. ``BaseException`` subclasses
    (``KeyboardInterrupt``, ``SystemExit``, ``asyncio.CancelledError``)
    still propagate — those are operator-controlled signals, not
    market-data noise.
    """
    try:
        point = await ports.market_data.get_price_at(coingecko_id, submitted_at)
        price = float(point.price_usd)
    except Exception as e:
        # Non-fatal — the bet still submits. The receipt + /me will
        # render without an entry-price line. We surface the failure
        # type + message in the logs so an operator running ``make
        # dev-logs`` during a CoinGecko incident sees the impact
        # (and can distinguish a known ``MarketDataError`` from an
        # unexpected payload-shape change).
        _log.warning(
            "entry-price snapshot failed for %s at submit: %s: %s; persisting NULL",
            coingecko_id,
            type(e).__name__,
            e,
        )
        return None
    return price


def _to_draft_response(
    draft: ClaimDraft,
    asset_query: str | None,
    candidates: list[AssetCandidateDTO],
) -> ClaimDraftResponse:
    """Map a domain ``ClaimDraft`` to the wire ``ClaimDraftResponse``."""
    ef = draft.extracted_fields
    pm = draft.parse_metadata
    return ClaimDraftResponse(
        direction=draft.direction,
        horizon_seconds=draft.horizon,
        conviction=draft.conviction,
        thesis_text=draft.thesis_text,
        asset_query=asset_query,
        asset_candidates=candidates,
        extracted_fields=ExtractedFieldsDTO(
            schema_version=ef.schema_version,
            target_price_usd=ef.target_price_usd,
            calendar_date_hint=ef.calendar_date_hint,
            rumour_referenced=ef.rumour_referenced,
            named_entities=list(ef.named_entities),
        ),
        parse_metadata=ParseMetadataDTO(
            parsed_via=pm.parsed_via if pm else "freeform_llm",
            model=pm.model if pm else None,
            model_version=pm.model_version if pm else None,
            prompt_hash=pm.prompt_hash if pm else None,
            raw_user_turns=list(pm.raw_user_turns) if pm else [],
            confidences=dict(pm.confidences) if pm else {},
            extracted_at=pm.extracted_at if pm else None,
        ),
    )
