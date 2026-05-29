"""``recompute_source_reputation(source_id)`` — reactive reputation update.

Per Q-029 proposal (plan §4): no daily cron, no ``pg_cron``. The
resolver enqueues this job the moment an outcome lands, and the worker
recomputes that source's reputation against all their resolved
outcomes. Writes one new ``source_reputation_history`` row per
outcome — finer-grained than the daily cadence and gives the Day-60
ML pipeline (MVP §11.1.A) richer training data for free.

Cost at v0 (~100 claims/day x ~2-day avg horizon): ~100
recomputes/day. Each is a single ``SELECT outcomes WHERE source_id=$1``
(indexed) + one INSERT. Trivial.

This job is **append-only** — never UPDATE ``source_reputation_history``
(MVP §15 invariant 10 / Q-029 reinforcement). Each recompute writes a
new row with the current ``effective_at`` timestamp; readers ask for
``ORDER BY effective_at DESC LIMIT 1``.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("merlin.worker.jobs.recompute_source_reputation")


def recompute_source_reputation(source_id: str) -> None:
    """Sync entry point called by RQ; bridges to the async coroutine."""
    asyncio.run(_recompute_async(source_id))


async def _recompute_async(source_id: str) -> None:
    """Run the recompute for ``source_id`` and INSERT one history row.

    Sprint 1 wires this against the real ``outcomes`` and
    ``source_reputation_history`` tables. For now we raise so the
    integration test harness sees a clear "not yet implemented" rather
    than silently no-opping.
    """
    _log.info("recompute_source_reputation: source_id=%s", source_id)
    raise NotImplementedError(
        "recompute_source_reputation is wired against SQLAlchemy in Sprint 1; "
        "integration tests stub the DB calls via the fake repository fixture."
    )
