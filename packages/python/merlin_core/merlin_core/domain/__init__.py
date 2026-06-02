"""Merlin domain layer — entities, value objects, pure regret math.

Stdlib only. No third-party imports allowed (enforced by import-linter).
Anything that touches a database, a network, or the current time goes
through a port in `merlin_core.application.ports` instead.
"""

from merlin_core.domain.claim_draft import (
    AssetCandidate,
    ClaimDraft,
    Conviction,
    Direction,
    ExtractedFields,
    FieldConfidence,
    Horizon,
    ParsedVia,
    ParseMetadata,
)
from merlin_core.domain.scoring import (
    FLAT_THRESHOLD_PCT,
    accuracy_pct,
    is_correct_call,
    is_flat,
)

__all__ = [
    "FLAT_THRESHOLD_PCT",
    "AssetCandidate",
    "ClaimDraft",
    "Conviction",
    "Direction",
    "ExtractedFields",
    "FieldConfidence",
    "Horizon",
    "ParseMetadata",
    "ParsedVia",
    "accuracy_pct",
    "is_correct_call",
    "is_flat",
]
