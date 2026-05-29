"""Repository ports — Protocols for persistence-layer adapters.

Repository Protocols live in the **core** layer; concrete implementations
in ``merlin_infrastructure.db.repositories``. The application layer
depends only on these Protocols — adapters are wired at the edge by
``merlin_api.deps`` and ``merlin_worker.bootstrap``.

Sprint 1 ships the three Protocols needed to make the bot onboard users
for real:

* ``SourceRepositoryPort`` — leaderboard identity + Admiralty grading.
* ``SourceIdentityRepositoryPort`` — PII boundary (raw Telegram IDs).
* ``InviteRepositoryPort`` — invite-only onboarding gate.

Later sprints add ``ClaimRepositoryPort``, ``OutcomeRepositoryPort``,
``AttributionLedgerPort``, and friends. Every append-only repository
(MVP §15 invariants 7/8/10/12) deliberately does NOT expose an
``update()`` / ``delete()`` method.

All methods are async — every adapter is built on async SQLAlchemy.
``source_id`` is always the opaque ``'tg:<sha256>'`` string from
``merlin_core.domain.source_id.make_source_id``; raw Telegram IDs only
appear on ``SourceIdentityRepositoryPort``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

# ---------------------------------------------------------------------------
# Domain shapes used in repository signatures. Plain dataclasses so they're
# free of any ORM dependency — the application layer never sees SQLAlchemy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Read-side projection of a row in ``sources``.

    Frozen because consumers should never mutate a query result —
    persistence happens via repository methods, not by mutating the
    object you got back.
    """

    source_id: str
    display_name: str | None
    kind: str
    reliability: str
    credibility: int
    consent_version: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InviteRecord:
    """Read-side projection of a row in ``invites``."""

    id: UUID
    code: str
    created_by_source_id: str | None
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    redeemed_by_source_id: str | None
    revoked_at: datetime | None

    @property
    def is_redeemable(self) -> bool:
        """True iff this invite can still be consumed.

        A consumer should NOT trust this property in a race — the
        repository's redeem method does the actual conditional UPDATE
        with a ``WHERE redeemed_at IS NULL`` guard. This is convenient
        for logging and UI hints only.
        """
        return self.redeemed_at is None and self.revoked_at is None


# ---------------------------------------------------------------------------
# Errors — repositories raise these instead of leaking SQLAlchemy types.
# ---------------------------------------------------------------------------


class RepositoryError(Exception):
    """Base for repository-layer errors."""


class SourceAlreadyExistsError(RepositoryError):
    """Raised when an INSERT collides with an existing source.

    Bot retries / duplicate-redeem are the most common cause. The
    application layer maps this to either a 409 or an idempotent
    "you're already onboarded" response.
    """


class InviteNotFoundError(RepositoryError):
    """Raised when a code lookup misses or finds an unredeemable invite."""


class InviteAlreadyRedeemedError(RepositoryError):
    """Raised by the atomic redeem path when a code was consumed first."""


# ---------------------------------------------------------------------------
# Protocols — repository contracts. Adapter implementations live in
# ``merlin_infrastructure.db.repositories``.
# ---------------------------------------------------------------------------


class SourceRepositoryPort(Protocol):
    """Persist + read ``sources`` rows.

    Sprint 1 surface is intentionally tiny — just enough for the bot's
    ``/start`` onboarding and the admin-bootstrap flow. The reputation
    write path (Admiralty updates) lands when the worker writes
    outcomes.
    """

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        """Return the source if it exists, else ``None``."""
        ...

    async def insert(
        self,
        *,
        source_id: str,
        display_name: str | None,
        kind: str,
        consent_version: str,
    ) -> SourceRecord:
        """INSERT a new source row. Raises ``SourceAlreadyExistsError`` on PK clash.

        Admiralty fields are set to their zeroed defaults
        (``reliability='F'``, ``credibility=6``) — the DB-level
        ``server_default`` does the work; the repository doesn't pass
        them explicitly.
        """
        ...

    async def update_display_name(self, source_id: str, display_name: str) -> SourceRecord:
        """Set the leaderboard display name. Idempotent on equal value."""
        ...


class SourceIdentityRepositoryPort(Protocol):
    """The PII repository — raw Telegram IDs (MVP §13, §15 invariant 9).

    Only the onboarding use case writes here; everything else looks up
    by ``source_id``. Future Phase-2 work adds a GDPR-export read path.
    """

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        """Reverse-lookup: from a raw TG id, return the opaque source_id.

        Used by the redeem and admin-bootstrap endpoints to spot
        already-onboarded users (idempotent re-redeems).
        """
        ...

    async def insert(
        self,
        *,
        source_id: str,
        tg_user_id: int,
        tg_handle: str | None,
    ) -> None:
        """INSERT a new identity row.

        Caller must hold a TX with the matching ``sources.insert`` so
        the two writes commit together.
        """
        ...


class InviteRepositoryPort(Protocol):
    """Persist + atomically redeem ``invites`` rows."""

    async def get_redeemable_by_code(self, code: str) -> InviteRecord | None:
        """Look up a code that is still consumable.

        Filters: ``redeemed_at IS NULL AND revoked_at IS NULL AND
        expires_at > now()``. Returns ``None`` on miss — callers must
        translate that to the 404 the bot expects.
        """
        ...

    async def insert(
        self,
        *,
        code: str,
        created_by_source_id: str | None,
        expires_at: datetime,
    ) -> InviteRecord:
        """INSERT a new invite. Raises if ``code`` collides (UNIQUE)."""
        ...

    async def mark_redeemed(
        self,
        *,
        invite_id: UUID,
        redeemed_by_source_id: str,
        redeemed_at: datetime,
    ) -> bool:
        """Atomically mark an invite redeemed.

        Implemented as ``UPDATE ... WHERE id = :id AND redeemed_at IS
        NULL`` and returns True iff exactly one row was updated. Two
        racing redeems for the same code will see one True and one
        False — the loser raises ``InviteAlreadyRedeemedError`` at the
        application layer.
        """
        ...
