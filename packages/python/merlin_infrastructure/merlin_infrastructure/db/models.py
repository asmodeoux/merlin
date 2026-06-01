"""SQLAlchemy ORM models for Merlin's persistence layer.

Sprint 1 lands the *minimum* tables needed to make the bot do its job
end-to-end:

* ``sources`` — leaderboard identity + Admiralty fields (MVP §11.1.A
  + §13). Zeroed Admiralty (``reliability='F'``, ``credibility=6``) for
  every newly redeemed source — they earn their grade by being correct.
* ``source_identity`` — PII split (MVP §13 + §15 invariant 9). The
  *only* place a raw ``tg_user_id`` lives. Every other table uses the
  opaque ``source_id`` instead.
* ``invites`` — invite-only onboarding gate (MVP §6 + §14). One-time
  codes issued by admins, redeemed atomically inside
  ``POST /invites/redeem``.
* ``claims`` — every market call a user logs. Denormalized asset
  fields (``asset_coingecko_id`` / ``asset_symbol`` / ``asset_name``)
  live directly on the row until the normalized ``assets`` /
  ``asset_aliases`` tables land with the scoring worker. Carries the
  freeform-LLM audit columns (``parsed_via`` + ``extracted_fields`` +
  ``parse_metadata``) per Q-028.

Remaining tables (events, hypotheses, outcomes,
source_reputation_history, attribution_ledger, override_log, assets,
asset_aliases, waitlist_entries, ...) land alongside their use cases in
later sprints. The migration that creates them must keep this file as
the single source of truth and run ``alembic revision --autogenerate``
against ``Base.metadata`` once the new ``Mapped`` columns are declared.

Append-only invariants (MVP §15 invariants 7/8/10/12):

* ``claims`` (except ``edits`` + ``soft_deleted_at``), ``outcomes``,
  ``source_reputation_history``, ``attribution_ledger``, and
  ``override_log`` are append-only. None of those tables exist in
  Sprint 1's slice; when they're added the migration must ship the
  Postgres triggers documented in
  ``tests/integration/test_append_only_invariants.py``.

PII boundary (MVP §15 invariant 9 + MVP §13):

* Raw Telegram IDs live ONLY in ``source_identity``. Every cross-table
  reference uses ``sources.source_id`` (the opaque ``'tg:<sha256>'``
  handle derived in ``merlin_core.domain.source_id.make_source_id``).
  The ``source_identity`` table is therefore intentionally narrow — it
  holds the join key, the raw ID, and an optional handle, and nothing
  else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every Merlin ORM model.

    All Sprint 1+ models inherit from this. Alembic reads
    ``Base.metadata`` (wired in ``alembic/env.py``) — adding a model
    here is sufficient to make ``alembic revision --autogenerate``
    pick it up.
    """


# ---------------------------------------------------------------------------
# sources — leaderboard identity, Admiralty grading.
# ---------------------------------------------------------------------------


class Source(Base):
    """A graded contributor.

    ``source_id`` is the opaque handle (``'tg:<sha256>'``); every other
    table refers to a source by this column. The raw Telegram ID lives
    in ``source_identity`` and never appears here.

    Admiralty fields (``reliability`` A-F, ``credibility`` 1-6) start at
    the worst grade and improve as Outcomes resolve.

    ``kind`` discriminates ``'user'`` (invited contributor) from
    ``'admin'`` (bypassed invite via ``POST /sources/admin_bootstrap``).
    Internal-only — never leaks to the UI.
    """

    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    reliability: Mapped[str] = mapped_column(String(1), nullable=False, default="F")
    credibility: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    consent_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint("kind IN ('user', 'admin')", name="ck_sources_kind"),
        CheckConstraint(
            "reliability IN ('A','B','C','D','E','F')",
            name="ck_sources_reliability",
        ),
        CheckConstraint(
            "credibility BETWEEN 1 AND 6",
            name="ck_sources_credibility",
        ),
    )


# ---------------------------------------------------------------------------
# source_identity — PII boundary (MVP §13 + §15 invariant 9).
# ---------------------------------------------------------------------------


class SourceIdentity(Base):
    """The PII split.

    One row per source. Holds the raw Telegram identifiers; no other
    application code may read from this table outside the redemption
    and (Phase-2) GDPR-export use cases. Read-side workers join via
    ``source_id`` only.
    """

    __tablename__ = "source_identity"

    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    tg_handle: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_source_identity_tg_user_id", "tg_user_id"),)


# ---------------------------------------------------------------------------
# invites — invite-only onboarding gate (MVP §6 + §14).
# ---------------------------------------------------------------------------


class Invite(Base):
    """A redeemable invite code.

    Issued by an admin (``created_by_source_id``) via
    ``POST /invites``; consumed by exactly one new user via
    ``POST /invites/redeem``. Single-use: once ``redeemed_at`` is set,
    the row is dead (the redeem endpoint's WHERE clause filters it out).
    Revocation is supported via ``revoked_at`` for the (rare) case where
    an admin needs to invalidate a code they leaked.

    Codes are URL-safe random tokens (``secrets.token_urlsafe(8) ≈ 11
    chars``) — short enough to paste but with ~64 bits of entropy.
    Stored case-sensitively; ``UNIQUE(code)`` prevents accidental dupes.
    """

    __tablename__ = "invites"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_source_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    redeemed_by_source_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("code", name="uq_invites_code"),
        Index("ix_invites_code_redeemable", "code", "redeemed_at", "revoked_at"),
    )


# ---------------------------------------------------------------------------
# claims — every market call a user logs (MVP §13).
# ---------------------------------------------------------------------------


class Claim(Base):
    """A single market call.

    Denormalized asset fields (``asset_coingecko_id`` /
    ``asset_symbol`` / ``asset_name``) live directly on the row; the
    normalized ``assets`` / ``asset_aliases`` tables land alongside the
    scoring worker in a later sprint. Until then, copying the three
    fields per claim is the simplest honest slice — re-normalising
    later is a column-copy migration.

    Append-only in spirit: the application layer never UPDATEs or
    DELETEs a claim outside the explicit edits / soft-delete columns
    that ship with the scoring sprint. The DB-level append-only
    triggers from MVP §15 invariants 7/8 land alongside the outcomes
    table; until then this is enforced by the repository layer
    omitting an ``update()`` method.
    """

    __tablename__ = "claims"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_coingecko_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    conviction: Mapped[str] = mapped_column(String(16), nullable=False)
    thesis_text: Mapped[str] = mapped_column(Text, nullable=False)
    asset_query: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolves_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Best-effort entry-price snapshot captured by ``POST /claims`` via
    # ``MarketDataPort.get_price_at(submitted_at)``. NULL when CoinGecko
    # was unavailable at submit (we never block a bet on market data)
    # and for legacy rows that predate this column.
    price_usd_at_submit: Mapped[float | None] = mapped_column(
        Double(),
        nullable=True,
    )
    parsed_via: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="form",
        server_default=text("'form'"),
    )
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    parse_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_claims_direction",
        ),
        CheckConstraint(
            "conviction IN ('weak','medium','strong','very_strong')",
            name="ck_claims_conviction",
        ),
        CheckConstraint(
            "parsed_via IN ('freeform_llm', 'form')",
            name="ck_claims_parsed_via",
        ),
        Index("ix_claims_source_resolves", "source_id", "resolves_at"),
        Index("ix_claims_source", "source_id"),
    )
