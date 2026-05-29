"""Ports — Protocol definitions for the adapters in merlin_infrastructure.

Append-only invariants (MVP_SPEC §15 invariants 7/8/10/12):

   Repository implementations for `claims` (except `edits` JSONB +
   `soft_deleted_at`), `outcomes`, `source_reputation_history`,
   `attribution_ledger`, and `override_log` MUST NOT expose an
   ``update()`` method — only ``insert_*_event()``-flavored calls.
   Postgres triggers in `merlin_infrastructure/alembic/versions/*` enforce
   this at the database layer as a defense-in-depth second line.

   `tests/integration/test_append_only_invariants.py` IS the contract: any
   future migration that removes a trigger fails CI immediately. AI code
   generators see this rule in the docstring above — please respect it.

Outcome supersedes chain (MVP §15 invariant 6):

   ``OutcomeRepositoryPort.latest_for_event(event_id)`` returns the head
   of the supersedes chain (the most recent outcome that is not itself
   superseded). New outcomes that correct a stale resolution INSERT a new
   row with ``supersedes_outcome_id`` set to the previous head's id — they
   do not UPDATE the existing row.

Extracted-fields immutability (MVP §15 invariant 17 — Q-028):

   ``claims.extracted_fields`` is frozen at claim commit. Phase 2
   verifiers may *read* it to extend resolution (target prices,
   calendar gates) but must never mutate it. Re-resolution writes a
   new ``outcomes`` row via the supersedes chain — the original claim
   remains pristine. Enforced at the repository layer (no update path
   for ``extracted_fields``) and via a Postgres column trigger.
"""

from merlin_core.application.ports.claim_drafter import (
    ClaimDrafterError,
    ClaimDrafterPort,
)
from merlin_core.application.ports.job_queue import JobQueuePort
from merlin_core.application.ports.market_data import (
    AssetMetadata,
    AssetNotFoundError,
    MarketDataError,
    MarketDataPort,
    PricePoint,
)
from merlin_core.application.ports.notifier import NotifierPort
from merlin_core.application.ports.repositories import (
    InviteAlreadyRedeemedError,
    InviteNotFoundError,
    InviteRecord,
    InviteRepositoryPort,
    RepositoryError,
    SourceAlreadyExistsError,
    SourceIdentityRepositoryPort,
    SourceRecord,
    SourceRepositoryPort,
)

__all__ = [
    "AssetMetadata",
    "AssetNotFoundError",
    "ClaimDrafterError",
    "ClaimDrafterPort",
    "InviteAlreadyRedeemedError",
    "InviteNotFoundError",
    "InviteRecord",
    "InviteRepositoryPort",
    "JobQueuePort",
    "MarketDataError",
    "MarketDataPort",
    "NotifierPort",
    "PricePoint",
    "RepositoryError",
    "SourceAlreadyExistsError",
    "SourceIdentityRepositoryPort",
    "SourceRecord",
    "SourceRepositoryPort",
]
