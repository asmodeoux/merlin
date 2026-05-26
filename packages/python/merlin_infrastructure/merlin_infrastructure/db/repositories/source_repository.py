"""SourceRepository — opaque source_id lookups.

Holds Source rows keyed by ``source_id`` (the opaque
``'tg:<sha256(...)>'`` handle from
``merlin_core.domain.source_id.make_source_id``). The raw Telegram
user-id lives in `SourceIdentityRepository` only (PII separation per
MVP §13).

Sprint 1 implements the methods declared in
`merlin_core.application.ports.SourceRepositoryPort`.
"""

from __future__ import annotations


class SourceRepository:
    """Sprint 1 — implement SourceRepositoryPort."""

    def __init__(self) -> None:
        """Sprint 1 will inject the async SQLAlchemy session here."""
        raise NotImplementedError("Sprint 1 — wire SQLAlchemy session + queries")
