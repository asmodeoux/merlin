"""``recompute_source_reputation(source_id)`` — reactive reputation update.

Per Q-029 proposal (plan §4): no daily cron, no ``pg_cron``. The
resolver enqueues this job the moment an outcome lands, and the worker
recomputes that source's reputation against all their resolved
outcomes. Writes one new ``source_reputation_history`` row per
recompute — finer-grained than the daily cadence and gives the Day-60
ML pipeline (MVP §11.1.A) richer training data for free.

Cost at v0 (~100 claims/day x ~2-day avg horizon): ~100
recomputes/day. Each is one ``SELECT`` (outcomes ⋈ claims for the
source) + one INSERT. Trivial.

This job is **append-only** — never UPDATE ``source_reputation_history``
(MVP §15 invariant 10 / Q-029 reinforcement). Each recompute writes a
new row with the current ``effective_at`` timestamp; readers ask for
``ORDER BY effective_at DESC LIMIT 1``.

Three numbers land on every snapshot, computed via the shared
``merlin_core.domain.scoring`` helpers so the bot, the API, and this
worker all agree by construction:

* ``accuracy_pct`` — plain hit rate. Numerator: ``is_correct=True``
  count. Denominator: resolved-status outcomes count.
* ``weighted_pnl_pct`` — true conviction-weighted directional return.
  ``Σ(directional_return x conviction_weight) / Σ(conviction_weight)``.
  Sits in the range of underlying % moves so "PnL" reads literally on
  /board / /me; high-conviction bets pull the average toward their
  results. Flat threshold is NOT applied here.
* ``sort_score`` — Bayesian-shrunk PnL,
  ``weighted_pnl_pct x n / (n + 5)``. Drives the /board ORDER BY.
  Cold-start: ``0.0`` (NOT NULL), sorts cold-start sources to the
  middle of the pack, not to ``-inf``.

User-locked choices that shape this computation:

* ``flat_handling = wash_zero`` for hit rate — flats counted as
  resolved but NOT correct.
* ``flat_handling = honest_signed`` for PnL — flats contribute their
  actual signed value x weight, not 0.
* ``min_calls_for_rank = show_all`` — no gate here; the shrinkage
  handles small-sample discipline; surfacing decisions live with the
  readers.
* ``store_redundant = yes_denorm`` — all three numbers are stored so
  the read path is a single ``SELECT`` with no per-row CASE math.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

_log = logging.getLogger("merlin.worker.jobs.recompute_source_reputation")


def recompute_source_reputation(source_id: str) -> None:
    """Sync entry point called by RQ; bridges to the async coroutine.

    The async return value is discarded here — RQ-driven recomputes
    are fire-and-forget. The resolver's inline call path
    (``_resolve_outcome_async``) awaits the coroutine directly to get
    the new PnL for the user notification.
    """
    asyncio.run(_recompute_async(source_id))


async def _recompute_async(source_id: str) -> float | None:
    """Run the recompute for ``source_id`` and INSERT one history row.

    One query fetches every resolved outcome (joined to its claim for
    direction + conviction); one INSERT writes the snapshot. The job
    is intentionally chatty in INFO logs so a live debugging session
    can confirm "the right number of bets contributed the right
    contributions" without reaching for psql.

    Returns the freshly-computed ``weighted_pnl_pct`` — ``None`` on
    cold-start (no resolved outcomes), the signed % otherwise. The
    resolver's inline call uses this return value to include the
    user's updated PnL in their outcome notification. Callers that
    don't care about the value (e.g. the RQ-driven sync entry point
    above) can discard it.

    Cold-start handling: zero resolved outcomes still INSERTs a row
    (``outcomes_resolved=0``, ``accuracy_pct=NULL``,
    ``weighted_pnl_pct=NULL``, ``sort_score=0.0``) so the audit trail
    captures every recompute — including those that fired right after
    a Q-023 rejection that left the source's tally untouched.
    """
    from merlin_core.domain.scoring import (
        accuracy_pct as compute_accuracy_pct,
    )
    from merlin_core.domain.scoring import (
        conviction_weight,
        directional_return_pct,
        shrunk_sort_score,
        weighted_mean_pnl_pct,
    )
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

        rows = await outcomes.fetch_resolved_for_reputation(source_id)
        resolved = len(rows)
        correct = sum(1 for r in rows if r.is_correct)
        accuracy = compute_accuracy_pct(correct, resolved)

        # ``contributions`` and ``weights`` are produced as a pair so
        # ``weighted_mean_pnl_pct`` can compute Σ(r x w) / Σ(w) without
        # recovering one from the other (the alternative — passing only
        # the products and deriving weights — would lose precision and
        # invite future drift if the per-bet contribution definition
        # changes).
        weights = [conviction_weight(r.conviction) for r in rows]
        contributions = [
            directional_return_pct(r.direction, r.observed_return_pct) * w
            for r, w in zip(rows, weights, strict=True)
        ]
        pnl_pct = weighted_mean_pnl_pct(contributions, weights)
        sort_score = shrunk_sort_score(pnl_pct, resolved)

        _log.info(
            "recompute_source_reputation: source=%s resolved=%d correct=%d "
            "accuracy=%s pnl_pct=%s sort_score=%.4f",
            source_id,
            resolved,
            correct,
            f"{accuracy:.2f}%" if accuracy is not None else "n/a",
            f"{pnl_pct:+.2f}%" if pnl_pct is not None else "n/a",
            sort_score,
        )

        await reputation.insert_snapshot(
            source_id=source_id,
            outcomes_resolved=resolved,
            outcomes_correct=correct,
            accuracy_pct=accuracy,
            weighted_pnl_pct=pnl_pct,
            sort_score=sort_score,
            # v0 doesn't track which outcome triggered which recompute
            # (we'd need to thread the outcome_id through the enqueue
            # call). Left NULL; the column exists so a future plumbing
            # pass can fill it in without a migration.
            triggering_outcome_id=None,
            effective_at=_dt.datetime.now(tz=_dt.UTC),
        )
        await session.commit()

        return pnl_pct
