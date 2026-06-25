"""``EngagementRepository`` — activation-nudge targeting + history (Q-031).

Two operations back the first-call nudge cron:

* ``list_first_call_nudge_targets`` — the eligibility query. One SELECT
  over ``sources`` with anti-joins to ``claims`` (zero bets) and
  ``engagement_nudges`` (cadence + lifetime cap). Returns opaque
  ``source_id`` strings only; the cron maps them to Telegram ids via
  the PII repository at send time.
* ``record_nudge`` — append one ``engagement_nudges`` row. Treated
  append-only (no update/delete), mirroring the canonical append-only
  repositories even though this operational table ships without the
  DB-level trigger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select

from merlin_infrastructure.db.models import Claim, EngagementNudge, Source

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: The only ``nudge_kind`` written at v0. Named so the job, the repo,
#: and the tests can't drift on the string literal.
FIRST_CALL_NUDGE_KIND = "first_call"


class EngagementRepository:
    """SQLAlchemy adapter for engagement-nudge targeting + history.

    Caller-managed TX (same contract as the other repositories): the
    nudge job commits after each ``record_nudge`` so the cap/cooldown
    state is durable the instant the DM is attempted.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def list_first_call_nudge_targets(
        self,
        *,
        cooldown_seconds: int,
        max_sends: int,
        min_account_age_seconds: int,
        limit: int,
        nudge_kind: str = FIRST_CALL_NUDGE_KIND,
    ) -> list[str]:
        """Sources eligible for a first-call nudge (Q-031).

        A source qualifies iff ALL of:

        * ``kind = 'user'`` — never nudge the admin account.
        * ``display_name IS NOT NULL`` — they finished onboarding and
          picked a name (the "signed up + named" gate, rule 1).
        * ``created_at`` older than ``min_account_age_seconds`` — a
          settle-in guard so we never nudge someone seconds after they
          set their name, mid-first-call.
        * NO rows in ``claims`` — they've made zero bets (rule 1).
        * Fewer than ``max_sends`` ``engagement_nudges`` rows of this
          kind — the lifetime cap (rule 3).
        * No ``engagement_nudges`` row of this kind within
          ``cooldown_seconds`` — the per-user spacing (rule 2).

        Ordered oldest-account-first (the longest-quiet testers get
        nudged first) and bounded by ``limit``. Returns opaque
        ``source_id`` strings; the cron resolves them to Telegram ids
        through ``source_identity`` (PII boundary, MVP §15 invariant 9).
        """
        now = datetime.now(tz=UTC)
        age_cutoff = now - timedelta(seconds=min_account_age_seconds)
        cooldown_cutoff = now - timedelta(seconds=cooldown_seconds)

        no_claims = ~exists().where(Claim.source_id == Source.source_id)
        nudge_count = (
            select(func.count())
            .select_from(EngagementNudge)
            .where(
                EngagementNudge.source_id == Source.source_id,
                EngagementNudge.nudge_kind == nudge_kind,
            )
            .scalar_subquery()
        )
        nudged_recently = exists().where(
            EngagementNudge.source_id == Source.source_id,
            EngagementNudge.nudge_kind == nudge_kind,
            EngagementNudge.sent_at > cooldown_cutoff,
        )

        stmt = (
            select(Source.source_id)
            .where(
                Source.kind == "user",
                Source.display_name.is_not(None),
                Source.created_at < age_cutoff,
                no_claims,
                nudge_count < max_sends,
                ~nudged_recently,
            )
            .order_by(Source.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [row[0] for row in result.all()]

    async def record_nudge(
        self,
        *,
        source_id: str,
        nudge_kind: str = FIRST_CALL_NUDGE_KIND,
    ) -> None:
        """INSERT one nudge-sent row. Flushes; the caller commits."""
        row = EngagementNudge(source_id=source_id, nudge_kind=nudge_kind)
        self._session.add(row)
        await self._session.flush()
