"""Add freeform-LLM columns to ``claims`` (Q-028).

Adds three columns enabling the freeform-LLM ingest path described in the
end-to-end bot-flow plan and locked in ``docs/DECISIONS.md`` Q-028:

- ``parsed_via`` — discriminator: ``'freeform_llm'`` or ``'form'``.
- ``extracted_fields`` JSONB — append-only, user-attested intent the v0
  verifier doesn't read but Phase 2 verifiers may extend resolution
  against (target prices, calendar dates, named entities, ...). Frozen
  at claim commit per MVP §15 invariant 17.
- ``parse_metadata`` JSONB — append-only audit trail of how the claim
  was constructed: ``{model, model_version, prompt_hash, raw_user_turns,
  confidences, extracted_at}``. The original user message lives at
  ``parse_metadata.raw_user_turns[0]`` and is immutable.

This migration is **defensive**: it inspects whether ``claims`` exists
before issuing ``ALTER TABLE``. The initial schema migration that
creates ``claims`` from MVP §13 is a separate Sprint 1 deliverable;
when it lands ahead of this one in the chain (via ``down_revision``),
the conditional becomes a no-op fast-path. If this migration runs
against an empty database (initial schema not yet applied), it logs
and returns — the deltas are reapplied when the initial migration
lands and this one re-runs (which is safe because the columns are
``ADD COLUMN IF NOT EXISTS``).

Revision ID: a1f7c3e8b2d4
Revises:
Create Date: 2026-05-29 10:00:00 UTC
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "a1f7c3e8b2d4"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_log = logging.getLogger("alembic.runtime.migration")


def _claims_exists() -> bool:
    """Return True iff the ``claims`` table is present in the current schema."""
    bind = op.get_bind()
    return bind.execute(sa.text("SELECT to_regclass('public.claims')")).scalar() is not None


def upgrade() -> None:
    """Add ``parsed_via``, ``extracted_fields``, ``parse_metadata`` to ``claims``."""
    if not _claims_exists():
        _log.info(
            "claims table not present yet; freeform-LLM columns will be added "
            "when the initial schema migration lands ahead of this revision."
        )
        return

    op.execute(
        """
        ALTER TABLE claims
          ADD COLUMN IF NOT EXISTS parsed_via TEXT
            NOT NULL DEFAULT 'form'
            CHECK (parsed_via IN ('freeform_llm', 'form')),
          ADD COLUMN IF NOT EXISTS extracted_fields JSONB
            NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS parse_metadata JSONB
            NOT NULL DEFAULT '{}'::jsonb
        """
    )
    # Drop the column default once existing rows are backfilled — new rows
    # must specify their parser explicitly so we don't silently mis-attribute.
    op.execute("ALTER TABLE claims ALTER COLUMN parsed_via DROP DEFAULT")


def downgrade() -> None:
    """Drop the three columns. Safe because they're append-only data we own."""
    if not _claims_exists():
        return
    op.execute(
        """
        ALTER TABLE claims
          DROP COLUMN IF EXISTS parse_metadata,
          DROP COLUMN IF EXISTS extracted_fields,
          DROP COLUMN IF EXISTS parsed_via
        """
    )
