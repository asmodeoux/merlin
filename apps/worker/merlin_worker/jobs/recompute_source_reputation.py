"""``recompute_source_reputation(source_id)`` — reactive reputation update.

Per Q-029 proposal (plan §4): no daily cron, no ``pg_cron``. The
resolver enqueues this job the moment an outcome lands, and the worker
recomputes that source's reputation against all their resolved
outcomes. Writes one new ``source_reputation_history`` row per
recompute — finer-grained than the daily cadence and gives the Day-60
ML pipeline (MVP §11.1.A) richer training data for free.

Cost at v0 (~100 claims/day x ~2-day avg horizon): ~100
recomputes/day. Each is a single ``SELECT outcomes WHERE source_id=$1``
(indexed) + one INSERT. Trivial.

This job is **append-only** — never UPDATE ``source_reputation_history``
(MVP §15 invariant 10 / Q-029 reinforcement). Each recompute writes a
new row with the current ``effective_at`` timestamp; readers ask for
``ORDER BY effective_at DESC LIMIT 1``.

v0 reputation == plain hit rate. User-locked choices that shape this
computation:

* ``flat_handling = wash_zero`` — flats counted as resolved but NOT
  correct. ``OutcomeRepository.count_resolved_and_correct_for_source``
  filters on ``status='resolved'`` (Q-023 rejection rows don't count)
  and sums ``is_correct=True``, so a flat row contributes ``+1`` to
  resolved and ``+0`` to correct.
* ``min_calls_for_rank = show_all`` — no gate here; surfacing
  decisions live with ``/board`` and ``/me`` readers.
* ``store_redundant = yes_denorm`` — ``accuracy_pct`` is stored
  alongside the counts so the leaderboard query doesn't recompute
  it per row.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

_log = logging.getLogger("merlin.worker.jobs.recompute_source_reputation")


def recompute_source_reputation(source_id: str) -> None:
    """Sync entry point called by RQ; bridges to the async coroutine."""
    asyncio.run(_recompute_async(source_id))


async def _recompute_async(source_id: str) -> None:
    """Run the recompute for ``source_id`` and INSERT one history row.

    Single query for the counts; single INSERT for the snapshot. If
    the source has zero resolved outcomes we still INSERT a row
    (``outcomes_resolved=0``, ``accuracy_pct=NULL``) so the audit
    trail captures every recompute, including those that fired right
    after a Q-023 rejection that left the source's tally untouched.
    """
    from merlin_core.domain.scoring import accuracy_pct as compute_accuracy_pct
    from merlin_infrastructure.db.repositories import (
        OutcomeRepository,
        SourceReputationRepository,
    )
    from merlin_infrastructure.db.session import get_session_factory

    from merlin_worker.config import Settings

    settings = Settings()
    session_factory = get_session_factory(settings.database_url)

    async with session_factory() as session:
        outcomes = OutcomeRepository(session)
        reputation = SourceReputationRepository(session)

        resolved, correct = await outcomes.count_resolved_and_correct_for_source(
            source_id,
        )
        accuracy = compute_accuracy_pct(correct, resolved)

        _log.info(
            "recompute_source_reputation: source=%s resolved=%d correct=%d accuracy=%s",
            source_id,
            resolved,
            correct,
            f"{accuracy:.2f}%" if accuracy is not None else "n/a",
        )

        await reputation.insert_snapshot(
            source_id=source_id,
            outcomes_resolved=resolved,
            outcomes_correct=correct,
            accuracy_pct=accuracy,
            # v0 doesn't track which outcome triggered which recompute
            # (we'd need to thread the outcome_id through the enqueue
            # call). Left NULL; the column exists so a future plumbing
            # pass can fill it in without a migration.
            triggering_outcome_id=None,
            effective_at=_dt.datetime.now(tz=_dt.UTC),
        )
        await session.commit()
