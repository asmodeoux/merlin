"""SQLAlchemy ORM models — Sprint 1 fills in the full schema from MVP §11.

This module is currently a stub. The first real revision lands in Sprint 1
via `make db-revision m='initial schema'`.

The stub below documents one critical model header — `Outcome` — to lock
in the ML-reservation fields from MVP_SPEC §11.1.D, §11.1.H, and §13 so
the first migration does not omit them. The remaining tables (`Source`,
`SourceIdentity`, `Claim`, `Asset`, `AssetAlias`, `Invite`, `Redemption`,
`SourceReputationHistory`, `AttributionLedger`, `OverrideLog`,
`WaitlistEntry`, ...) are added alongside their repositories in Sprint 1.

Append-only invariants (MVP §15 invariants 7/8/10/12) — `claims` (except
`edits` JSONB + `soft_deleted_at`), `outcomes`, `source_reputation_history`,
`attribution_ledger`, `override_log`. Enforced both at the repository
layer (no `update()` method) and via Postgres triggers (initial Alembic
migration).
"""

from __future__ import annotations

# NOTE: imports stay commented until Sprint 1 wires SQLAlchemy in. Leaving
# uncommented imports here would (a) break mypy --strict against an empty
# package, and (b) suggest schema decisions we haven't made yet.

# from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
#
# class Base(DeclarativeBase):
#     """Declarative base for all Merlin ORM models."""


# ---------------------------------------------------------------------------
# Outcome stub header — MVP §11.1.D + §11.1.H + §13 ML-reservation fields.
# ---------------------------------------------------------------------------
#
# Sprint 1 must implement the Outcome model with at minimum the columns
# listed below. Listing them here as comments so the first migration is
# unambiguous and AI code generators don't drop fields.
#
# class Outcome(Base):
#     __tablename__ = "outcomes"
#
#     # Identity + linkage
#     id:                            Mapped[UUID]           # pk
#     claim_id:                      Mapped[UUID]           # fk -> claims
#     event_id:                      Mapped[UUID]           # de-dupe key per (claim, t0)
#     created_at:                    Mapped[datetime]
#
#     # Resolution outcome
#     outcome_return_4dp:            Mapped[Decimal]        # 4-decimal return per MVP §12.10
#     pnl_if_followed_usd:           Mapped[Decimal]
#     pnl_if_ignored_usd:            Mapped[Decimal] = 0    # MVP §15 invariant 3: default 0
#     # regret_usd is DERIVED (= pnl_if_followed - pnl_if_ignored); MVP §15 invariant 3
#     # means we NEVER store it as a canonical column. Computed via VIEW or in the
#     # repository read path.
#
#     # ML reservation fields — MVP §11.1.D + §11.1.H + §13
#     intermediate_returns:          Mapped[list[Decimal]]  # JSONB of intra-horizon checkpoints
#     max_favorable_excursion:       Mapped[Decimal]        # MFE over the horizon
#     max_adverse_excursion:         Mapped[Decimal]        # MAE over the horizon
#     benchmark_return:              Mapped[Decimal]        # market/BTC benchmark over same window
#     resolution_candle_t0_ref:      Mapped[str]            # opaque ref to the t0 candle
#     resolution_candle_resolve_ref: Mapped[str]            # opaque ref to the resolve candle
#
#     # Supersedes chain — MVP §15 invariant 6
#     supersedes_outcome_id:         Mapped[UUID | None]    # head-of-chain when NULL
