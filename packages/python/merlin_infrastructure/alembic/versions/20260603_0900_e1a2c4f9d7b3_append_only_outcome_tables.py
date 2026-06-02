"""Create the four append-only tables that drive outcome scoring.

Sprint-1 lived without them because the bot path ended at "claim
persisted + worker job enqueued". The first live 2-hour BTC bet showed
the cost: the worker fires, ``_ClaimContext.load()`` raises
``NotImplementedError``, the user gets no message. This migration ships
the destination tables so the resolver + reputation jobs have something
honest to INSERT into:

* ``outcomes`` — one row per resolution. The resolver INSERTs
  ``status='resolved'`` (happy path), ``status='rejected_asset_mutation'``
  (Q-023), or ``status='resolution_failed_final'`` (retries exhausted).
  Carries the denormalised ``is_correct`` flag so the reputation
  aggregation doesn't need to re-derive correctness per row.

* ``source_reputation_history`` — append-only snapshots written by the
  ``recompute_source_reputation`` job. One row per recompute. Readers
  ask for ``ORDER BY effective_at DESC LIMIT 1`` per source.

* ``attribution_ledger`` — empty in this migration. Pre-creating the
  table lets the append-only triggers apply cleanly now so the future
  attribution worker can't accidentally land on a non-trigger-protected
  table.

* ``override_log`` — operator-action audit trail. Sprint 1 uses the
  ``make admin-rerun`` path which INSERTs a row here per intervention.

Append-only invariants enforced at the DB layer (MVP §15 invariants
7/8/10/12). PL/pgSQL triggers reject every UPDATE / DELETE on
``outcomes``, ``source_reputation_history``, ``attribution_ledger``,
and ``override_log``. ``claims`` keeps its existing
"UPDATE allowed only on ``edits`` JSONB / ``soft_deleted_at``" policy
which the application layer already honours by omission; we don't add
the trigger for ``claims`` here because the columns it WOULD let
through don't exist yet (they ship with the closed-loop sprint).

Revision ID: e1a2c4f9d7b3
Revises: d9f4c2b8a1e6
Create Date: 2026-06-03 09:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "e1a2c4f9d7b3"
down_revision: str | None = "d9f4c2b8a1e6"
branch_labels: str | None = None
depends_on: str | None = None


# The append-only tables this migration creates. The trigger function
# is parameterised on the table name so we only ship one definition
# and apply it four times.
_APPEND_ONLY_TABLES: tuple[str, ...] = (
    "outcomes",
    "source_reputation_history",
    "attribution_ledger",
    "override_log",
)


def _create_outcomes() -> None:
    op.create_table(
        "outcomes",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "claim_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_return_pct", sa.Double(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("price_t0_usd", sa.Double(), nullable=True),
        sa.Column("price_resolve_usd", sa.Double(), nullable=True),
        sa.Column("price_t0_ref", sa.String(length=64), nullable=True),
        sa.Column("price_resolve_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "supersedes_outcome_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("outcomes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('resolved','rejected_asset_mutation','resolution_failed_final')",
            name="ck_outcomes_status",
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (observed_return_pct IS NOT NULL)",
            name="ck_outcomes_resolved_has_return",
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (is_correct IS NOT NULL)",
            name="ck_outcomes_resolved_has_is_correct",
        ),
    )
    op.create_index(
        "ix_outcomes_source_resolved",
        "outcomes",
        ["source_id", "resolved_at"],
    )
    op.create_index("ix_outcomes_claim", "outcomes", ["claim_id"])


def _create_source_reputation_history() -> None:
    op.create_table(
        "source_reputation_history",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("outcomes_resolved", sa.Integer(), nullable=False),
        sa.Column("outcomes_correct", sa.Integer(), nullable=False),
        sa.Column("accuracy_pct", sa.Double(), nullable=True),
        sa.Column(
            "triggering_outcome_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("outcomes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcomes_resolved >= 0",
            name="ck_srh_outcomes_resolved_nonneg",
        ),
        sa.CheckConstraint(
            "outcomes_correct >= 0 AND outcomes_correct <= outcomes_resolved",
            name="ck_srh_outcomes_correct_bounded",
        ),
        sa.CheckConstraint(
            "(outcomes_resolved = 0) = (accuracy_pct IS NULL)",
            name="ck_srh_accuracy_null_iff_cold_start",
        ),
    )
    op.create_index(
        "ix_srh_source_effective",
        "source_reputation_history",
        ["source_id", "effective_at"],
    )


def _create_attribution_ledger() -> None:
    op.create_table(
        "attribution_ledger",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "claim_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("asset_coingecko_id", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("is_first_mover", sa.Boolean(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_attribution_direction",
        ),
    )
    op.create_index(
        "ix_attribution_asset_direction",
        "attribution_ledger",
        ["asset_coingecko_id", "direction", "recorded_at"],
    )


def _create_override_log() -> None:
    op.create_table(
        "override_log",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "target_claim_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "target_outcome_id",
            PgUUID(as_uuid=True),
            sa.ForeignKey("outcomes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_override_log_actor_recorded",
        "override_log",
        ["actor_source_id", "recorded_at"],
    )


def _install_append_only_triggers() -> None:
    """PL/pgSQL trigger that raises on every UPDATE / DELETE.

    One function definition; one BEFORE-UPDATE + one BEFORE-DELETE
    trigger per table. The function name encodes "append-only" so an
    operator inspecting ``\\df+`` knows immediately what they're
    looking at. We don't parameterise the table inside the function —
    the error message just identifies the operation and the
    ``TG_TABLE_NAME`` PostgreSQL exposes at trigger time, which is
    enough to give an actionable message when an INSERTs-only
    invariant is violated.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION merlin_reject_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only invariant: % on % is not allowed (MVP §15)',
                TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                FOR EACH ROW
                EXECUTE FUNCTION merlin_reject_mutation();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_delete
                BEFORE DELETE ON {table}
                FOR EACH ROW
                EXECUTE FUNCTION merlin_reject_mutation();
            """
        )


def upgrade() -> None:
    """Create the four tables + their indexes + their append-only triggers.

    Order matters — ``outcomes`` is referenced by
    ``source_reputation_history.triggering_outcome_id`` and
    ``override_log.target_outcome_id``, so it has to land first.
    """
    _create_outcomes()
    _create_source_reputation_history()
    _create_attribution_ledger()
    _create_override_log()
    _install_append_only_triggers()


def downgrade() -> None:
    """Reverse upgrade: drop triggers, then the function, then tables.

    Triggers go before their tables (PG complains otherwise on the
    detached function in some edge cases); the shared function goes
    next; tables drop in reverse-creation order to satisfy the FK
    chain.
    """
    for table in _APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table}")
    op.execute("DROP FUNCTION IF EXISTS merlin_reject_mutation()")
    op.drop_index("ix_override_log_actor_recorded", table_name="override_log")
    op.drop_table("override_log")
    op.drop_index(
        "ix_attribution_asset_direction",
        table_name="attribution_ledger",
    )
    op.drop_table("attribution_ledger")
    op.drop_index(
        "ix_srh_source_effective",
        table_name="source_reputation_history",
    )
    op.drop_table("source_reputation_history")
    op.drop_index("ix_outcomes_claim", table_name="outcomes")
    op.drop_index("ix_outcomes_source_resolved", table_name="outcomes")
    op.drop_table("outcomes")
