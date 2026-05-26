"""SourceIdentityRepository — PII table (raw Telegram user IDs).

The ONLY repository that touches the `source_identity` table. Access is
gated by admin-only API routes (Sprint 3) so day-to-day reads of a Source
do not pull the PII. MVP §13 + MVP §15 invariant 9.

Sprint 1 implements the methods declared in
`merlin_core.application.ports.SourceIdentityRepositoryPort`.
"""

from __future__ import annotations


class SourceIdentityRepository:
    """Sprint 1 — implement SourceIdentityRepositoryPort."""

    def __init__(self) -> None:
        """Sprint 1 will inject the async SQLAlchemy session here."""
        raise NotImplementedError("Sprint 1 — wire SQLAlchemy session + queries")
