"""Add conviction-weighted PnL and Bayesian sort score to source_reputation_history.

Sprint-1 reputation was plain hit rate. The first live resolved bet
(``cbe4b3bb`` — BTC long, -1.3%) read on /board as "0% accuracy" and
on /me as "tracking your first resolutions". Both were technically
honest but visually punishing: a single -1.3% loss on a 2h bet does
not equal "this user has zero skill". The conviction-weighted PnL +
shrinkage redesign (locked in the design conversation) lifts the
leaderboard from a boolean game to a magnitude-aware one. New columns
on ``source_reputation_history``:

* ``weighted_pnl_pct`` — ``Σ(directional_return × conviction_weight) / n``.
  Signed, nullable cold-start (same NULL contract as ``accuracy_pct``).
  Honest signed values: a +0.3% win at weak conviction contributes
  +0.3% × 1 = +0.3%; the flat threshold is NOT applied here (it stays
  exclusive to the hit-rate boolean).

* ``sort_score`` — ``weighted_pnl_pct × n / (n + 5)``, Bayesian
  shrinkage with k=5. NOT NULL because cold-start sources sort to
  the middle of the pack (score=0.0), not to ``-inf``. Driver of
  ``/board ORDER BY``; never rendered to the user.

CHECK constraint: ``(outcomes_resolved = 0) = (weighted_pnl_pct IS NULL)``
mirrors the existing ``accuracy_pct`` rule so a buggy recompute can't
write a PnL for a source with no resolved bets.

Index on ``sort_score`` keeps the read path index-only — the read
already filters by latest ``effective_at`` per source via
``ix_srh_source_effective``; this complements it.

Backfill policy: any row with ``outcomes_resolved > 0`` predates
this migration's PnL math, so we have no honest per-bet number to
write into ``weighted_pnl_pct``. Two acceptable answers were:

* Leave ``NULL`` and relax the CHECK to only require "cold-start
  rows have NULL". Asymmetric — defeats the point of the symmetric
  invariant used by ``accuracy_pct``.
* Backfill with ``0.0`` and let the next ``recompute_source_reputation``
  invocation (triggered by the next resolved outcome, or by
  ``make admin-rerun``) overwrite with the real number on its next
  INSERT. This keeps the CHECK strict from day one.

We pick the second option. ``0.0`` is honest as "no PnL data
captured here" rather than a misleading "wins / losses cancelled
out"; ``/me`` and ``/board`` still read the LATEST snapshot per
source, so the legacy row is shadowed the moment the recompute
runs against the new schema. ``sort_score`` defaults to ``0.0``
via the column's ``server_default`` — no separate UPDATE needed.

Revision ID: f2b5d6e7c180
Revises: e1a2c4f9d7b3
Create Date: 2026-06-04 10:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2b5d6e7c180"
down_revision = "e1a2c4f9d7b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the two columns + backfill + the index + the CHECK constraint.

    Order matters in three places:

    1. Add ``weighted_pnl_pct`` (nullable) BEFORE the CHECK that
       references it.
    2. Backfill ``weighted_pnl_pct = 0.0`` for any pre-existing row
       with ``outcomes_resolved > 0`` BEFORE the CHECK fires, or the
       constraint addition fails with ``check constraint violated by
       some row``.
    3. Add the CHECK AFTER the backfill.

    The ``sort_score`` column ships with ``server_default='0'`` so
    existing rows satisfy the NOT NULL contract without a separate
    UPDATE pass.
    """
    op.add_column(
        "source_reputation_history",
        sa.Column("weighted_pnl_pct", sa.Double(), nullable=True),
    )
    op.add_column(
        "source_reputation_history",
        sa.Column(
            "sort_score",
            sa.Double(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Backfill — see module docstring "Backfill policy". The
    # append-only ``trg_source_reputation_history_no_update`` trigger
    # rejects every UPDATE at runtime, so for this one-shot migration
    # backfill we DISABLE it, UPDATE the pre-existing legacy rows,
    # then re-ENABLE it. This is the *only* legitimate path past the
    # invariant: it lives inside a transactional DDL block, so any
    # failure rolls the trigger state back automatically (we never
    # leak an unprotected window outside the migration TX).
    #
    # 0.0 is the honest placeholder for "no PnL data captured here";
    # the next recompute INSERTs a real number on the next snapshot,
    # shadowing this legacy row at read time. NOT applied to
    # ``outcomes_resolved = 0`` rows — those keep ``weighted_pnl_pct
    # IS NULL`` to match ``accuracy_pct IS NULL`` (the cold-start
    # signal readers depend on).
    op.execute(
        "ALTER TABLE source_reputation_history "
        "DISABLE TRIGGER trg_source_reputation_history_no_update"
    )
    op.execute(
        "UPDATE source_reputation_history "
        "SET weighted_pnl_pct = 0.0 "
        "WHERE outcomes_resolved > 0 AND weighted_pnl_pct IS NULL"
    )
    op.execute(
        "ALTER TABLE source_reputation_history "
        "ENABLE TRIGGER trg_source_reputation_history_no_update"
    )
    op.create_check_constraint(
        "ck_srh_pnl_null_iff_cold_start",
        "source_reputation_history",
        "(outcomes_resolved = 0) = (weighted_pnl_pct IS NULL)",
    )
    op.create_index(
        "ix_srh_sort_score",
        "source_reputation_history",
        ["sort_score"],
    )


def downgrade() -> None:
    """Drop in reverse order. Index first (so the column drop doesn't
    have to cascade), then the constraint, then the columns. Mirrors
    upgrade() for a clean round-trip — the migration parity preflight
    asserts every upgrade has a downgrade that drops what it created.
    """
    op.drop_index("ix_srh_sort_score", table_name="source_reputation_history")
    op.drop_constraint(
        "ck_srh_pnl_null_iff_cold_start",
        "source_reputation_history",
        type_="check",
    )
    op.drop_column("source_reputation_history", "sort_score")
    op.drop_column("source_reputation_history", "weighted_pnl_pct")
