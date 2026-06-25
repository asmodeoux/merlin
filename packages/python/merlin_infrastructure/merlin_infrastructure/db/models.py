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
    Boolean,
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
    # Horizon is stored as integer seconds (continuous, not a Literal).
    # Validated at the API boundary to ``[HORIZON_MIN_SECONDS,
    # HORIZON_MAX_SECONDS]`` (1h..90d). BIGINT not INT because the
    # column shape is a duration count and BIGINT is the conventional
    # type for that — future range increases never collide with the
    # type ceiling.
    horizon_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
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


# ---------------------------------------------------------------------------
# outcomes — one row per resolution event (MVP §13 + §15 invariants 7/8).
# ---------------------------------------------------------------------------


class Outcome(Base):
    """A resolved outcome for a single claim — append-only.

    The resolver INSERTs one row per ``resolve_outcome`` execution.
    Corrections happen by INSERTing a new row with
    ``supersedes_outcome_id`` pointing at the previous head; readers
    of "latest outcome for this claim" walk the supersedes chain
    (MVP §15 invariant 6). The application layer never UPDATEs or
    DELETEs an outcome row (invariants 7/8) — the migration ships
    PL/pgSQL triggers that enforce this at the database too.

    ``status`` discriminates the three end states the resolver can
    write:

    * ``'resolved'`` — the canonical happy path. Outcome row carries
      a real ``observed_return_pct`` and an ``is_correct`` flag.
    * ``'rejected_asset_mutation'`` — Q-023. CoinGecko 404'd the
      asset between ``t0`` and ``t_resolve``. ``observed_return_pct``
      is NULL; the row exists for auditability and to make the
      resolver idempotent (subsequent retries see the row and
      short-circuit).
    * ``'resolution_failed_final'`` — every RQ retry exhausted. The
      operator-rerun path INSERTs a corrective row later.

    ``is_correct`` is denormalised — yes, it's derivable from
    ``observed_return_pct`` + the claim's ``direction``, but the
    reputation aggregation query reads ten thousand rows at a time
    and the join+CASE is an order of magnitude slower than reading
    a precomputed boolean. The resolver writes both columns in the
    same INSERT so they cannot drift (see ``is_correct_call`` in
    ``merlin_core.domain.scoring`` — the single source of the rule).
    """

    __tablename__ = "outcomes"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_return_pct: Mapped[float | None] = mapped_column(
        Double(),
        nullable=True,
    )
    is_correct: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    price_t0_usd: Mapped[float | None] = mapped_column(Double(), nullable=True)
    price_resolve_usd: Mapped[float | None] = mapped_column(Double(), nullable=True)
    price_t0_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price_resolve_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_outcome_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("outcomes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('resolved','rejected_asset_mutation','resolution_failed_final')",
            name="ck_outcomes_status",
        ),
        # ``is_correct`` is meaningful only on ``status='resolved'``
        # rows. Rejected / failed rows must leave it NULL so any
        # downstream reader that filters ``is_correct IS TRUE`` does
        # the right thing without having to JOIN ``status`` too.
        CheckConstraint(
            "(status = 'resolved') = (observed_return_pct IS NOT NULL)",
            name="ck_outcomes_resolved_has_return",
        ),
        CheckConstraint(
            "(status = 'resolved') = (is_correct IS NOT NULL)",
            name="ck_outcomes_resolved_has_is_correct",
        ),
        Index("ix_outcomes_source_resolved", "source_id", "resolved_at"),
        Index("ix_outcomes_claim", "claim_id"),
    )


# ---------------------------------------------------------------------------
# source_reputation_history — append-only reputation snapshots (MVP §11.1.A).
# ---------------------------------------------------------------------------


class SourceReputationHistory(Base):
    """One row per recompute — append-only (MVP §15 invariant 10).

    Q-029 keeps reputation reactive: the resolver enqueues a
    ``recompute_source_reputation`` job after each outcome lands, the
    job counts the source's resolved + correct outcomes, computes the
    conviction-weighted PnL + the Bayesian-shrunk sort score, and
    INSERTs one row capturing the snapshot. Readers ask for
    ``ORDER BY effective_at DESC LIMIT 1`` per source.

    v0 columns capture three layered views of the same history:

    * Hit rate (``outcomes_correct / outcomes_resolved``,
      stored as ``accuracy_pct``) — the boolean "did the call land?"
      view.
    * Conviction-weighted PnL (``weighted_pnl_pct``) — magnitude +
      direction, signed, with each bet's contribution scaled by its
      stated conviction (weak=1x, medium=3x, strong=5x, very_strong=7x).
      Honest about signed magnitude; flat threshold NOT applied.
    * Sort score (``sort_score``) — Bayesian-shrunk PnL,
      ``weighted_pnl_pct x n / (n + 5)``, used as the leaderboard ORDER
      BY key. Demotes lucky one-shots; lets a consistent grinder
      compete; sinks spammers + active losers naturally. NOT rendered
      to users.

    User-locked design choices that shape the columns:

    * ``flat_handling = wash_zero`` for hit rate: ``outcomes_resolved``
      counts every non-mutation outcome (won + lost + flat). Only
      ``outcomes_correct`` excludes flats.
    * ``flat_handling = honest_signed`` for PnL: ``weighted_pnl_pct``
      counts the actual signed value of every resolved bet — a +0.3%
      win still contributes +0.3% x weight.
    * ``min_calls_for_rank = show_all``: no minimum-N gate at the
      data layer — the Bayesian shrinkage handles small-sample
      discipline; surfacing decisions live with the readers.
    * ``store_redundant = yes_denorm``: ``accuracy_pct``,
      ``weighted_pnl_pct``, and ``sort_score`` are stored so the
      ``/board`` read path is a single ``SELECT`` (no per-row CASE
      maths, no joins to ``outcomes``).
    """

    __tablename__ = "source_reputation_history"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    outcomes_resolved: Mapped[int] = mapped_column(Integer, nullable=False)
    outcomes_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``NULL`` when ``outcomes_resolved = 0`` — the cold-start signal
    # that ``/me`` / ``/board`` need to distinguish "no data yet" from
    # an honest 0%.
    accuracy_pct: Mapped[float | None] = mapped_column(Double(), nullable=True)
    # Conviction-weighted PnL %: true weighted mean
    # ``Σ(directional_return x weight) / Σ(weight)``. Sits in the range
    # of underlying % moves so "PnL" reads literally on /board and /me.
    # ``NULL`` cold-start, same rule as ``accuracy_pct``. Signed: + means
    # the source's calls paid off on aggregate.
    weighted_pnl_pct: Mapped[float | None] = mapped_column(Double(), nullable=True)
    # Bayesian-shrunk sort key used by /board: ``weighted_pnl_pct x n /
    # (n + 5)``. Stored so ``ORDER BY sort_score DESC`` is a plain
    # index hit rather than per-row arithmetic. ``0.0`` on cold-start
    # (not NULL) because we sort cold-start sources to the middle of
    # the pack, not to the top / bottom.
    sort_score: Mapped[float] = mapped_column(
        Double(),
        nullable=False,
        server_default=text("0"),
    )
    triggering_outcome_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("outcomes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "outcomes_resolved >= 0",
            name="ck_srh_outcomes_resolved_nonneg",
        ),
        CheckConstraint(
            "outcomes_correct >= 0 AND outcomes_correct <= outcomes_resolved",
            name="ck_srh_outcomes_correct_bounded",
        ),
        CheckConstraint(
            "(outcomes_resolved = 0) = (accuracy_pct IS NULL)",
            name="ck_srh_accuracy_null_iff_cold_start",
        ),
        # Same null-iff-cold-start rule as accuracy_pct — pinning at
        # the DB level so a buggy recompute can't store a PnL number
        # for a source with no resolved bets.
        CheckConstraint(
            "(outcomes_resolved = 0) = (weighted_pnl_pct IS NULL)",
            name="ck_srh_pnl_null_iff_cold_start",
        ),
        Index(
            "ix_srh_source_effective",
            "source_id",
            "effective_at",
        ),
        # ORDER BY sort_score DESC is the /board hot path. Compound
        # index with effective_at lets the read path pick the latest
        # snapshot per source and then sort, both via the index.
        Index(
            "ix_srh_sort_score",
            "sort_score",
        ),
    )


# ---------------------------------------------------------------------------
# attribution_ledger — append-only "who said it first" record (MVP §15 inv 12).
# ---------------------------------------------------------------------------


class AttributionLedger(Base):
    """First-mover credit per (asset_coingecko_id, direction, window).

    Reserved for the attribution feature (MVP §11.4). Sprint 1 ships
    the table empty so the append-only triggers can apply cleanly and
    the reputation pipeline has its destination ready when the
    attribution worker lands.

    Append-only (MVP §15 invariant 12) — every fact is a fresh row.
    """

    __tablename__ = "attribution_ledger"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_coingecko_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    is_first_mover: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_attribution_direction",
        ),
        Index(
            "ix_attribution_asset_direction",
            "asset_coingecko_id",
            "direction",
            "recorded_at",
        ),
    )


# ---------------------------------------------------------------------------
# override_log — append-only operator-action trail (MVP §15 invariant 11/12).
# ---------------------------------------------------------------------------


class OverrideLog(Base):
    """Operator override events — append-only audit trail.

    Manual outcome correction (the ``make admin-rerun`` path),
    forced-resolve interventions, and the future bot-side
    ``/admin/dispute`` flow all INSERT a row here capturing what the
    operator did and why. No row is ever UPDATEd or DELETEd
    (MVP §15 invariant 11) so an external audit can replay the full
    history of human intervention.
    """

    __tablename__ = "override_log"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    actor_source_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_claim_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=True,
    )
    target_outcome_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("outcomes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_override_log_actor_recorded", "actor_source_id", "recorded_at"),)


# ---------------------------------------------------------------------------
# engagement_nudges — outbound activation DMs we've sent (Q-031).
# ---------------------------------------------------------------------------


class EngagementNudge(Base):
    """One row per engagement nudge DM sent to a source (Q-031).

    Backs the first-call nudge cron's per-user limits without trusting
    Redis (the same durability lesson as the Q-030 resolution sweep):

    * ``COUNT(*)`` per ``(source_id, nudge_kind)`` enforces the lifetime
      cap (max 2 first-call nudges per user).
    * ``MAX(sent_at)`` enforces the cooldown (at most one nudge per week
      per user).

    Operational table — NOT one of the MVP §15 append-only invariant
    tables, so it ships without the UPDATE/DELETE-rejecting trigger.
    The repository still treats it append-only (insert + read only),
    which is enough for an audit trail of who we've pinged.

    Stores the opaque ``source_id`` only — never a raw Telegram id
    (MVP §15 invariant 9). The cron maps ``source_id`` -> ``tg_user_id``
    through ``source_identity`` at send time and logs only the hash.
    """

    __tablename__ = "engagement_nudges"

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
    # Discriminates nudge campaigns so a future "you have an unresolved
    # bet" or "you've gone quiet" nudge tracks its own cap/cooldown
    # independently. v0 only writes ``'first_call'``.
    nudge_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_engagement_nudges_source_kind", "source_id", "nudge_kind"),)
