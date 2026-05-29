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

from datetime import UTC, datetime, timedelta
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
from merlin_core.domain import ClaimDraft
from merlin_core.domain.claim_draft import Horizon
from merlin_core.domain.horizons import HORIZON_SECONDS

from merlin_api.deps import Ports, get_ports

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
    summary="Submit a claim (atomic commit + scheduled resolution).",
    description=(
        "Five INSERTs + one RQ ``enqueue_at`` inside a single transaction. "
        "Returns the persisted claim with its resolve date. Idempotent "
        "against duplicate submissions by ``(source, asset, t0, horizon)``."
    ),
)
async def submit_claim(
    body: SubmitClaimRequest,
    ports: Ports = Depends(get_ports),
) -> ClaimResponse:
    """Commit a claim and schedule its resolution."""
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

    submitted_at = datetime.now(UTC)
    # body.horizon is a wire-format ``str`` per merlin_core.contracts; the
    # domain ``HORIZON_SECONDS`` map is keyed by the Literal alias. The
    # FastAPI body schema enforces membership, so the cast is safe.
    horizon_key: Horizon = body.horizon  # type: ignore[assignment]
    resolves_at = submitted_at + timedelta(seconds=HORIZON_SECONDS[horizon_key])
    claim_id = uuid4()

    # Sprint 1 wires the SubmitClaim use case (DB INSERTs) here. Until
    # then, we still enqueue the resolution job so the queue
    # contract test (api → RQ → worker) is exercised end-to-end.
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

    return ClaimResponse(
        claim_id=claim_id,
        asset_symbol=metadata.symbol,
        direction=body.direction,
        horizon=body.horizon,
        conviction=body.conviction,
        thesis_text=body.thesis_text,
        submitted_at=submitted_at,
        resolves_at=resolves_at,
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
        horizon=draft.horizon,
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
