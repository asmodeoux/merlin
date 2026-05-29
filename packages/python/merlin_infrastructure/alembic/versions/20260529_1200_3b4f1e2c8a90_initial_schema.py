"""Initial schema — sources, source_identity, invites.

The minimum DB surface needed to make the bot onboard users for real:

* ``sources`` — leaderboard identity + Admiralty grading (MVP §11.1.A).
* ``source_identity`` — PII split (MVP §13, §15 invariant 9).
* ``invites`` — one-time codes (MVP §6 + §14).

Extensions:

* ``pgcrypto`` — required for ``gen_random_uuid()`` (the invite PK
  default). Postgres 13+ ships this built in but the migration creates
  it idempotently in case the dev's image disabled it.

Schema decisions that should survive future migrations:

* ``source_id`` is a string ``'tg:<sha256[:32]>'`` (64-char cap) — never
  a UUID. Anything that ends up storing it must use ``String(64)``
  and reference ``sources.source_id`` as the FK target, not a bigint.
* Admiralty CHECK constraints land here so the DB rejects a typo
  before it ever reaches the repository layer. Removing them is a
  deliberate decision, not a stylistic one.

Append-only triggers: NOT in this migration. The append-only tables
(``claims``, ``outcomes``, ``source_reputation_history``,
``attribution_ledger``, ``override_log``) don't exist yet — they land
with their triggers in the next migration that creates them.

Revision ID: 3b4f1e2c8a90
Revises:
Create Date: 2026-05-29 12:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "3b4f1e2c8a90"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create extensions + the three foundational tables."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "sources",
        sa.Column("source_id", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.String(length=40), nullable=True),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
        sa.Column(
            "reliability",
            sa.String(length=1),
            nullable=False,
            server_default=sa.text("'F'"),
        ),
        sa.Column(
            "credibility",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("6"),
        ),
        sa.Column(
            "consent_version",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'v0'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("kind IN ('user', 'admin')", name="ck_sources_kind"),
        sa.CheckConstraint(
            "reliability IN ('A','B','C','D','E','F')",
            name="ck_sources_reliability",
        ),
        sa.CheckConstraint(
            "credibility BETWEEN 1 AND 6",
            name="ck_sources_credibility",
        ),
    )

    op.create_table(
        "source_identity",
        sa.Column(
            "source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_handle", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tg_user_id", name="uq_source_identity_tg_user_id"),
    )
    op.create_index(
        "ix_source_identity_tg_user_id",
        "source_identity",
        ["tg_user_id"],
    )

    op.create_table(
        "invites",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "created_by_source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "redeemed_by_source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_invites_code"),
    )
    op.create_index(
        "ix_invites_code_redeemable",
        "invites",
        ["code", "redeemed_at", "revoked_at"],
    )


def downgrade() -> None:
    """Drop the three tables in dependency order."""
    op.drop_index("ix_invites_code_redeemable", table_name="invites")
    op.drop_table("invites")
    op.drop_index("ix_source_identity_tg_user_id", table_name="source_identity")
    op.drop_table("source_identity")
    op.drop_table("sources")
