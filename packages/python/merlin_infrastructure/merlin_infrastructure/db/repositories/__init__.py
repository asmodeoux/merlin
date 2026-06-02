"""Repository implementations of the Protocols in merlin_core.application.ports.

Append-only repositories (`outcome_repository`, `source_reputation_history`,
`attribution_ledger`, `override_log`, and the closed-loop columns of
`claims`) MUST NOT expose an `update()` or `delete()` method — only
`insert_*_event()`-flavored writes. Postgres triggers in the initial
Alembic migration enforce this at the database layer as the second line
of defense.

See MVP §15 invariants 7/8/10/12 and
`tests/integration/test_append_only_invariants.py`.
"""

from merlin_infrastructure.db.repositories.claim_repository import ClaimRepository
from merlin_infrastructure.db.repositories.invite_repository import InviteRepository
from merlin_infrastructure.db.repositories.outcome_repository import OutcomeRepository
from merlin_infrastructure.db.repositories.source_identity_repository import (
    SourceIdentityRepository,
)
from merlin_infrastructure.db.repositories.source_repository import SourceRepository
from merlin_infrastructure.db.repositories.source_reputation_repository import (
    SourceReputationRepository,
)

__all__ = [
    "ClaimRepository",
    "InviteRepository",
    "OutcomeRepository",
    "SourceIdentityRepository",
    "SourceRepository",
    "SourceReputationRepository",
]
