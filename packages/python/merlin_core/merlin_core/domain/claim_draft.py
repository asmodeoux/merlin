"""Domain types for the freeform-LLM claim-draft path (Q-028).

Stdlib only — no Pydantic, no SQLAlchemy. These are the canonical shapes
the application layer reasons about; the wire-format dual lives in
``merlin_core.contracts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Direction = Literal["long", "short"]
Horizon = Literal["1h", "4h", "24h", "3d", "7d", "14d", "30d", "90d"]
Conviction = Literal["weak", "medium", "strong", "very_strong"]
ParsedVia = Literal["freeform_llm", "form"]


@dataclass(frozen=True, slots=True)
class AssetCandidate:
    """One row from a CoinGecko ``/search`` proxy response.

    Surfaced to the user as a button when the LLM's asset confidence is
    below the auto-pick threshold, or when the user clicks "Edit asset".
    """

    coingecko_id: str
    symbol: str
    name: str
    market_cap_rank: int | None = None


@dataclass(frozen=True, slots=True)
class FieldConfidence:
    """LLM-reported confidence in a single extracted field.

    The drafter doesn't make policy decisions — it reports values + how
    sure it is. The use case decides what to do with low-confidence
    fields (typically: surface as ``missing_fields`` so the bot asks).
    """

    value: object
    confidence: float


@dataclass(frozen=True, slots=True)
class ExtractedFields:
    """Append-only intent the user attests to via the preview Confirm.

    Fields the v0 verifier doesn't score (``target_price_usd``,
    ``calendar_date_hint``, ...) live here and are frozen at claim
    commit per MVP §15 invariant 17. Phase 2 verifiers may extend
    resolution against them without a schema migration.

    ``schema_version`` is mandatory — Phase 2 may reinterpret a field
    and we want deterministic versioned reads.
    """

    schema_version: int = 1
    target_price_usd: float | None = None
    calendar_date_hint: str | None = None  # ISO date or natural-language
    rumour_referenced: bool = False
    named_entities: tuple[str, ...] = ()
    # Loose tail for fields the LLM extracts but we haven't typed yet.
    # Phase 2 promotes promising patterns into named fields above.
    raw_extras: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseMetadata:
    """Audit trail of how a claim was constructed.

    ``raw_user_turns[0]`` is the original immutable freeform message
    that triggered the LLM extraction. Never overwritten even if the
    user later edits the thesis on preview. See "Original-message
    preservation" in the bot-flow plan.
    """

    parsed_via: ParsedVia
    model: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    raw_user_turns: tuple[str, ...] = ()
    confidences: dict[str, float] = field(default_factory=dict)
    extracted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimDraft:
    """The output of ``ClaimDrafterPort.draft(text)``.

    Pure structural snapshot of what the LLM extracted, plus the
    candidate assets the API resolved against CoinGecko ``/search``.
    The bot renders this on the preview; the user confirms or edits;
    the use case ``submit_claim`` consumes the final shape and writes
    the ``Claim`` row.

    All fields are optional because the LLM may be unable to extract
    any single one with confidence. ``missing_fields`` enumerates
    which ones the bot must ask about (or, in the edit-not-clarify
    flow, surface as still-needed before Confirm is enabled).
    """

    asset_candidates: tuple[AssetCandidate, ...] = ()
    direction: Direction | None = None
    horizon: Horizon | None = None
    conviction: Conviction | None = None
    thesis_text: str | None = None
    extracted_fields: ExtractedFields = field(default_factory=ExtractedFields)
    parse_metadata: ParseMetadata | None = None
    missing_fields: tuple[str, ...] = ()
