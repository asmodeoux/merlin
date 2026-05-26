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
"""
