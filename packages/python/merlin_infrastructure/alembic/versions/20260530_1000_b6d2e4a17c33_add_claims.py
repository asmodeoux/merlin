"""Add ``claims`` — the minimum table needed to make /me + /board honest.

Sprint-1 follow-up. After the initial schema landed (sources +
source_identity + invites), the bot's ``/alpha`` Confirm path had a
latent gap: it enqueued a resolution job but never persisted the
underlying call. Anything ``/me`` returned would have been a lie. This
migration adds the ``claims`` table so the bot can show truthful open
calls.

Scope choices (deliberate):

* **Denormalized assets.** ``asset_coingecko_id`` / ``asset_symbol`` /
  ``asset_name`` live on the claim row directly. The normalized
  ``assets`` / ``asset_aliases`` tables land alongside the scoring
  worker in a later sprint — until then storing the CoinGecko id +
  symbol per claim is the simplest honest slice. Re-normalising later
  is a column-copy migration, not a data loss.
* **No outcomes / events / hypotheses / attribution_ledger yet.**
  Those are append-only tables (MVP §15 invariants 7/8/10/12) that
  ship with their Postgres triggers; they're scored by the worker.
  The minimum slice for ``/me`` is "show what's open"; "what's
  resolved" arrives with the worker.
* **Closed-loop columns (``edits`` JSONB, ``soft_deleted_at``)** are
  intentionally NOT in this migration — they belong with the
  scoring/resolution sprint's append-only triggers.
* **Freeform-LLM columns folded in here.** A defensive earlier
  ``a1f7c3e8b2d4_add_freeform_llm_columns_to_claims`` migration
  pre-dated this one in the chain; it was a no-op against an empty DB.
  We add ``parsed_via`` + ``extracted_fields`` + ``parse_metadata``
  inline so the chain stays linear and there's one source of truth
  for the column set.

Constraints:

* ``direction`` CHECKed in (``'long'``, ``'short'``).
* ``conviction`` CHECKed in (``'weak'``, ``'medium'``, ``'strong'``,
  ``'very_strong'``).
* ``parsed_via`` CHECKed in (``'freeform_llm'``, ``'form'``) per Q-028.
* ``source_id`` FK → ``sources.source_id`` (CASCADE on source delete —
  not used yet, but matches what GDPR export will need).

Indexes:

* ``(source_id, resolves_at)`` — the ``/me`` open-call query
  (``WHERE source_id = :sid AND resolves_at > now()``).
* ``source_id`` standalone — the ``count_resolved_by_source`` query
  and Phase-2 leaderboard aggregations.

Revision ID: b6d2e4a17c33
Revises: 3b4f1e2c8a90
Create Date: 2026-05-30 10:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "b6d2e4a17c33"
down_revision: str | None = "3b4f1e2c8a90"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the ``claims`` table + its two indexes."""
    op.create_table(
        "claims",
        sa.Column(
            "id",
            PgUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            sa.String(length=64),
            sa.ForeignKey("sources.source_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_coingecko_id", sa.String(length=128), nullable=False),
        sa.Column("asset_symbol", sa.String(length=32), nullable=False),
        sa.Column("asset_name", sa.String(length=128), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("horizon", sa.String(length=8), nullable=False),
        sa.Column("conviction", sa.String(length=16), nullable=False),
        sa.Column("thesis_text", sa.Text(), nullable=False),
        sa.Column("asset_query", sa.String(length=128), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolves_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "parsed_via",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'form'"),
        ),
        sa.Column(
            "extracted_fields",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "parse_metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_claims_direction",
        ),
        sa.CheckConstraint(
            "conviction IN ('weak','medium','strong','very_strong')",
            name="ck_claims_conviction",
        ),
        sa.CheckConstraint(
            "parsed_via IN ('freeform_llm', 'form')",
            name="ck_claims_parsed_via",
        ),
    )

    op.create_index(
        "ix_claims_source_resolves",
        "claims",
        ["source_id", "resolves_at"],
    )
    op.create_index("ix_claims_source", "claims", ["source_id"])


def downgrade() -> None:
    """Drop the ``claims`` table + its indexes."""
    op.drop_index("ix_claims_source", table_name="claims")
    op.drop_index("ix_claims_source_resolves", table_name="claims")
    op.drop_table("claims")
