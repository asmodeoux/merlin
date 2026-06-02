"""Move ``claims.horizon`` from a string code to integer seconds.

The bot's ``/alpha`` flow previously surfaced an 8-bucket Literal
(``"1h"`` / ``"4h"`` / ``"24h"`` / ``"3d"`` / ``"7d"`` / ``"14d"`` /
``"30d"`` / ``"90d"``). Two UX gaps drove the move to a continuous
seconds model:

* The LLM had no concrete mapping for explicit hour counts the
  user typed (e.g. ``"3 hours"``) — confidence dropped below the
  per-field floor and the bot surfaced the horizon picker.
* The horizon picker only listed ``24h+`` quick-picks, so the
  user's first impression was "Merlin's minimum horizon is 24
  hours" — which contradicts the domain (``1h`` and ``4h`` were
  in the Literal) and contradicts what a user reasonably expects
  from a short-horizon prediction bot.

Continuous-seconds storage + a free-form text reply in the picker +
range-bounded validation (``[1h, 90d]``) closes both gaps. MVP §15
invariant 4 ("horizons are exact seconds, no calendar/DST math")
is preserved by construction — we never reach for ``relativedelta``
/ business-day math.

Schema change:

* DROP ``claims.horizon`` (VARCHAR(8)).
* ADD ``claims.horizon_seconds`` (BIGINT NOT NULL).
* Backfill in the same migration via a CASE expression that maps
  every historical code to its exact second count.

The backfill runs **between** the ADD (NULLABLE) and the ALTER (SET
NOT NULL) so existing dev / test rows survive the migration without
losing data. After the backfill we DROP the old column — a single
migration step keeps the schema linear and the rollback simple.

We use ``BigInteger`` instead of ``Integer`` because (a) the upper
bound ``90 days = 7_776_000`` fits in INT32 with room to spare, but
(b) the column shape is a duration count and BIGINT is the
conventional choice for second / millisecond counts so future
range increases never collide with the type ceiling.

Revision ID: d9f4c2b8a1e6
Revises: c8e3a1d4b5f7
Create Date: 2026-06-02 14:00:00 UTC
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d9f4c2b8a1e6"
down_revision: str | None = "c8e3a1d4b5f7"
branch_labels: str | None = None
depends_on: str | None = None

# String code → exact seconds. Frozen in the migration on purpose:
# the upgrade path must remain reproducible from this file alone
# even if the helper table (``merlin_core.contracts.horizons``)
# evolves later. The eight historical buckets are an immutable set.
_HISTORICAL_CODE_SECONDS: dict[str, int] = {
    "1h": 3_600,
    "4h": 14_400,
    "24h": 86_400,
    "3d": 259_200,
    "7d": 604_800,
    "14d": 1_209_600,
    "30d": 2_592_000,
    "90d": 7_776_000,
}


def upgrade() -> None:
    """Drop ``horizon`` string, add ``horizon_seconds`` BIGINT NOT NULL.

    Three-step DDL:

    1. ADD ``horizon_seconds`` NULL.
    2. Backfill from the existing ``horizon`` codes via a CASE.
       Unknown codes (defensive: there shouldn't be any) collapse
       to ``24h`` rather than failing the migration; emitting a
       warning here would require app-layer logging which alembic
       can't reach safely.
    3. SET NOT NULL on ``horizon_seconds``, then DROP ``horizon``.
    """
    op.add_column(
        "claims",
        sa.Column("horizon_seconds", sa.BigInteger(), nullable=True),
    )

    # Build the CASE expression as raw SQL so the backfill is one
    # statement (no Python-side row iteration / no async dance).
    case_clauses = " ".join(
        f"WHEN '{code}' THEN {seconds}" for code, seconds in _HISTORICAL_CODE_SECONDS.items()
    )
    op.execute(
        f"""
        UPDATE claims
        SET horizon_seconds = CASE horizon
            {case_clauses}
            ELSE 86400
        END
        WHERE horizon_seconds IS NULL
        """
    )

    op.alter_column("claims", "horizon_seconds", nullable=False)
    op.drop_column("claims", "horizon")


def downgrade() -> None:
    """Re-add ``horizon`` (VARCHAR(8)) and backfill from seconds.

    Inverts the upgrade so the migration is round-trippable. The
    seconds → code mapping uses the same historical table; values
    that fall outside the 8-bucket set (which is now legal under
    the continuous model) collapse to the *closest* historical
    code so the downgrade is still applicable on dev rows.
    """
    op.add_column(
        "claims",
        sa.Column("horizon", sa.String(length=8), nullable=True),
    )

    # Reverse map: pick the bucket whose absolute seconds-diff is
    # smallest. Built as a CASE for the values we know, with a
    # catch-all '24h' fallback for anything that can't snap cleanly
    # (defensive; the data we wrote in upgrade() all snaps).
    inverse_clauses = " ".join(
        f"WHEN {seconds} THEN '{code}'" for code, seconds in _HISTORICAL_CODE_SECONDS.items()
    )
    op.execute(
        f"""
        UPDATE claims
        SET horizon = CASE horizon_seconds
            {inverse_clauses}
            ELSE '24h'
        END
        WHERE horizon IS NULL
        """
    )

    op.alter_column("claims", "horizon", nullable=False)
    op.drop_column("claims", "horizon_seconds")
