"""Wire contracts — Pydantic schemas shared between `merlin_api` and `merlin_client`.

These are the *only* schemas the API surfaces and the *only* schemas the
Python SDK accepts/returns. The TypeScript client (Phase 2 React frontend)
regenerates from `docs/openapi.json` via `openapi-typescript` and stays in
sync the same way.

Layer rule (enforced by import-linter): this module imports stdlib +
pydantic only. No domain types leak across this boundary — anything that
needs to cross becomes a contract DTO here.
"""

from merlin_core.contracts.assets import (
    AssetCandidateDTO,
    AssetResolution,
    AssetSearchResponse,
    ResolveAssetRequest,
)
from merlin_core.contracts.board import BoardEntry, BoardResponse, MeResponse
from merlin_core.contracts.claims import (
    ClaimDraftRequest,
    ClaimDraftResponse,
    ClaimResponse,
    ExtractedFieldsDTO,
    ParseMetadataDTO,
    SubmitClaimRequest,
)
from merlin_core.contracts.horizons import (
    HORIZON_MAX_SECONDS,
    HORIZON_MIN_SECONDS,
    HORIZON_QUICK_PICKS,
    HorizonParseError,
    HorizonRangeError,
    horizon_to_timedelta,
    humanise_horizon,
    is_in_range,
    parse_horizon_text,
)
from merlin_core.contracts.invites import (
    CreateInviteRequest,
    CreateInviteResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RevokeInviteRequest,
    RevokeInviteResponse,
)
from merlin_core.contracts.me import UpdateMeRequest
from merlin_core.contracts.sources import (
    AdminBootstrapRequest,
    AdminBootstrapResponse,
)

__all__ = [
    "HORIZON_MAX_SECONDS",
    "HORIZON_MIN_SECONDS",
    "HORIZON_QUICK_PICKS",
    "AdminBootstrapRequest",
    "AdminBootstrapResponse",
    "AssetCandidateDTO",
    "AssetResolution",
    "AssetSearchResponse",
    "BoardEntry",
    "BoardResponse",
    "ClaimDraftRequest",
    "ClaimDraftResponse",
    "ClaimResponse",
    "CreateInviteRequest",
    "CreateInviteResponse",
    "ExtractedFieldsDTO",
    "HorizonParseError",
    "HorizonRangeError",
    "MeResponse",
    "ParseMetadataDTO",
    "RedeemInviteRequest",
    "RedeemInviteResponse",
    "ResolveAssetRequest",
    "RevokeInviteRequest",
    "RevokeInviteResponse",
    "SubmitClaimRequest",
    "UpdateMeRequest",
    "horizon_to_timedelta",
    "humanise_horizon",
    "is_in_range",
    "parse_horizon_text",
]
