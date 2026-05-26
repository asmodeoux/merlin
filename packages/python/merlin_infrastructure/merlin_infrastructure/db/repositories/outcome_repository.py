"""OutcomeRepository — append-only outcome events with supersedes chain.

Append-only (MVP §15 invariants 7, 8) — NO `update()` method. Corrections
to a stale outcome INSERT a new row with `supersedes_outcome_id` set to
the previous head, and `latest_for_event()` returns the head of the
chain (MVP §15 invariant 6).

Sprint 1 implements the methods declared in
`merlin_core.application.ports.OutcomeRepositoryPort`.
"""

from __future__ import annotations


class OutcomeRepository:
    """Sprint 1 — implement OutcomeRepositoryPort.

    Exposes:

    - `insert_outcome_event(...)` — append a new outcome row.
    - `latest_for_event(event_id)` — head of the supersedes chain.

    Notably does NOT expose `update()` or `delete()`. Postgres triggers
    on the `outcomes` table reject UPDATE/DELETE at the DB layer too.
    """

    def __init__(self) -> None:
        """Sprint 1 will inject the async SQLAlchemy session here."""
        raise NotImplementedError("Sprint 1 — wire SQLAlchemy session + queries")
