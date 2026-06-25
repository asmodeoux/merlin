"""Add ``engagement_nudges`` — durable cadence/cap state for activation DMs.

Q-031 adds a worker cron that DMs onboarded testers who have a name but
zero calls, prompting their first bet. The per-user limits (at most once
a week, at most twice ever) MUST survive restarts and must not be
fightable by Redis eviction — the exact failure class Q-030 just fixed
for resolution. So the nudge history lives in Postgres, one row per DM
sent:

* ``COUNT(*)`` per ``(source_id, nudge_kind)`` enforces the lifetime cap.
* ``MAX(sent_at)`` per ``(source_id, nudge_kind)`` enforces the cooldown.

Scope choices:

* **Operational, not an MVP §15 append-only table.** It ships WITHOUT
  the UPDATE/DELETE-rejecting trigger the canonical append-only tables
  carry (``outcomes`` / ``source_reputation_history`` /
  ``attribution_ledger`` / ``override_log``). The repository keeps it
  insert+read-only by convention; a stray UPDATE here is harmless
  (worst case a miscounted nudge), so the heavier DB-level guard isn't
  warranted.
* **``source_id`` only — no raw Telegram id.** PII stays in
  ``source_identity`` (MVP §15 invariant 9); the cron joins for the TG
  id at send time and never persists it here.
* **``nudge_kind`` discriminator** so future campaigns (e.g. a
  "you have a bet resolving soon" nudge) track their own cap/cooldown
  independently. v0 writes only ``'first_call'``.

Index ``(source_id, nudge_kind)`` serves both the count and the
recency subqueries the eligibility SELECT issues per candidate source.

Revision ID: a3c9f1e7d2b8
Revises: f2b5d6e7c180
Create Date: 2026-06-25 16:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "a3c9f1e7d2b8"
down_revision: str | None = "f2b5d6e7c180"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the ``engagement_nudges`` table + its lookup index."""
    op.create_table(
        "engagement_nudges",
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
        sa.Column("nudge_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_engagement_nudges_source_kind",
        "engagement_nudges",
        ["source_id", "nudge_kind"],
    )


def downgrade() -> None:
    """Drop the ``engagement_nudges`` table + its index."""
    op.drop_index("ix_engagement_nudges_source_kind", table_name="engagement_nudges")
    op.drop_table("engagement_nudges")
