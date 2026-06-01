"""Claim-related wire schemas.

Three top-level shapes:

- ``ClaimDraftRequest`` / ``ClaimDraftResponse`` — freeform-LLM extraction
  endpoint. ``POST /claims/draft`` is stateless: no DB writes, no auth
  side effects.
- ``SubmitClaimRequest`` — body of ``POST /claims``. Atomic: 5 INSERTs
  + 1 RQ enqueue inside one transaction.
- ``ClaimResponse`` — what ``GET /claims/{id}`` returns and what
  ``POST /claims`` returns on success.

The DTO shape of ``extracted_fields`` and ``parse_metadata`` mirrors
the domain types in ``merlin_core.domain.claim_draft`` 1:1; Pydantic is
required here because the layer crosses into wire format, where
domain dataclasses can't go.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from merlin_core.contracts.assets import AssetCandidateDTO

# Mirror the domain Literal aliases as plain strings here so the
# generated OpenAPI surface stays decoupled from the domain layer.
# import-linter forbids contracts/ from importing domain/ aliases.
Direction = str  # "long" | "short"
Horizon = str  # "1h" | "4h" | "24h" | "3d" | "7d" | "14d" | "30d" | "90d"
Conviction = str  # "weak" | "medium" | "strong" | "very_strong"
ParsedVia = str  # "freeform_llm" | "form"


class ClaimDraftRequest(BaseModel):
    """Body of ``POST /claims/draft`` — freeform LLM extraction."""

    text: str = Field(..., max_length=4_000, description="Raw user message; may be empty.")


class ExtractedFieldsDTO(BaseModel):
    """User-attested intent the v0 verifier doesn't score.

    Frozen at claim commit per MVP §15 invariant 17. The bot does
    **not** surface low-confidence fields on the preview — they live
    here for the record.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    target_price_usd: float | None = None
    calendar_date_hint: str | None = None
    rumour_referenced: bool = False
    named_entities: list[str] = Field(default_factory=list)


class ParseMetadataDTO(BaseModel):
    """Audit trail of how a claim was constructed.

    ``raw_user_turns[0]`` is the original freeform message and is
    immutable. ``model``, ``model_version``, and ``prompt_hash``
    together identify the LLM call exactly so re-runs are auditable.
    """

    model_config = ConfigDict(extra="allow")

    parsed_via: ParsedVia
    model: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    raw_user_turns: list[str] = Field(default_factory=list)
    confidences: dict[str, float] = Field(default_factory=dict)
    extracted_at: datetime | None = None


class ClaimDraftResponse(BaseModel):
    """Response of ``POST /claims/draft`` — prefilled fields + asset candidates."""

    direction: Direction | None = None
    horizon: Horizon | None = None
    conviction: Conviction | None = None
    thesis_text: str | None = None
    asset_query: str | None = None
    asset_candidates: list[AssetCandidateDTO] = Field(default_factory=list)
    extracted_fields: ExtractedFieldsDTO = Field(default_factory=ExtractedFieldsDTO)
    parse_metadata: ParseMetadataDTO


class SubmitClaimRequest(BaseModel):
    """Body of ``POST /claims`` — atomic commit.

    The bot composes this from the preview state (LLM-prefilled fields,
    possibly edited by the user, plus the final asset selection).

    ``tg_user_id`` rides the body for the same reason ``PATCH /me``
    needs it: the HMAC bearer authenticates the *service*, not the
    per-user identity, so the API has to be told which source is
    making the call. Resolved server-side via the
    ``source_identity`` table — the raw TG id never leaves this
    boundary and never ends up on the ``claims`` row itself.
    """

    coingecko_id: str = Field(..., min_length=1)
    direction: Direction
    horizon: Horizon
    conviction: Conviction
    thesis_text: str = Field(..., min_length=1, max_length=4_000)
    tg_user_id: int
    # Optional debug breadcrumb: the LLM-extracted short symbol the
    # bot used to populate CoinGecko's /search candidates. Persisted
    # on the claim row so an audit can reconstruct "user wrote X, LLM
    # said Y, we picked Z" without rerunning the LLM.
    asset_query: str | None = Field(default=None, max_length=128)
    extracted_fields: ExtractedFieldsDTO = Field(default_factory=ExtractedFieldsDTO)
    parse_metadata: ParseMetadataDTO


class ClaimResponse(BaseModel):
    """What ``POST /claims`` returns on success and ``GET /claims/{id}`` returns.

    Does **not** surface the v0 simulated conviction-to-dollar mapping
    or any internal scoring fields. The user sees their own attested
    inputs back, plus the claim id and the resolve date.
    """

    claim_id: UUID
    asset_symbol: str
    direction: Direction
    horizon: Horizon
    conviction: Conviction
    thesis_text: str
    submitted_at: datetime
    resolves_at: datetime
    # Best-effort entry-price snapshot in USD at ``submitted_at``.
    # ``None`` when CoinGecko was unreachable at submit (we never
    # block a bet on market data) or for legacy rows that predate
    # the column. The bot renders the price line iff this is set —
    # an absent price never invents a placeholder.
    price_usd_at_submit: float | None = None
    extracted_fields: ExtractedFieldsDTO
    # parse_metadata included for the admin / debug path; the bot
    # ignores it for normal display.
    parse_metadata: ParseMetadataDTO | None = None
    # Outcome attached when resolved; null while pending.
    outcome: dict[str, Any] | None = None
