"""Add ``claims.price_usd_at_submit`` — entry price snapshot.

Sprint-1 UX gap: the bot showed the user what they bet (asset +
direction + horizon + thesis) but never the price they were betting
against. Without an entry price, the user can't sanity-check the
call ("did I get the right asset?", "was the price already weird?")
and the eventual outcome ("BTC moved +3%") is meaningless without a
starting point.

This column lands the snapshot the API captures at ``POST /claims``
time. The route calls ``MarketDataPort.get_price_at(submitted_at)``
(CoinGecko ±5min bucket) and persists the result on the row. The
column is **NULLABLE** because:

* Backfill: existing pre-launch claims have no recorded entry price.
* Best-effort capture: CoinGecko transport failure at submit time
  must not block the bet. We log + persist NULL and the receipt /
  ``/me`` simply omit the price line — preserving the "market data
  is UX acceleration, never a system dependency" rule.
* Sprint-2 worker: when the resolution worker eventually populates
  ``outcomes`` it can fall back to ``get_price_at(submitted_at)``
  for legacy rows where this field is NULL.

``DOUBLE PRECISION`` matches the in-memory ``float`` shape returned
by ``MarketDataPort.PricePoint`` — keeping the type identical
end-to-end avoids precision drift on the audit trail. We do not use
``NUMERIC`` here because the upstream source (CoinGecko) is itself
``float``-shaped; promoting to ``NUMERIC`` would manufacture
precision that doesn't exist at the boundary.

No new index. The ``/me`` and receipt paths join through
``id`` / ``source_id`` (already indexed); a price-only index would
be dead weight at v0.

Revision ID: c8e3a1d4b5f7
Revises: b6d2e4a17c33
Create Date: 2026-06-01 23:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3a1d4b5f7"
down_revision: str | None = "b6d2e4a17c33"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``price_usd_at_submit`` column to ``claims``."""
    op.add_column(
        "claims",
        sa.Column(
            "price_usd_at_submit",
            sa.Double(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the ``price_usd_at_submit`` column."""
    op.drop_column("claims", "price_usd_at_submit")
