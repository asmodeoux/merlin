"""``recompute_source_reputation(source_id)`` — reactive reputation update.

Per Q-029 proposal: no daily cron, no ``pg_cron``. The
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from merlin_core.application.ports import SourceReputationRecord
    from sqlalchemy.ext.asyncio import AsyncSession

    from merlin_worker.config import Settings

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

        # Capture the source's previous head BEFORE inserting the new
        # snapshot — the board-change hook reverts this source to ``prev``
        # to reconstruct the pre-change board (see
        # ``_emit_board_change_notifications``). ``None`` on cold-start.
        prev = await reputation.latest_for_source(source_id)

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

        # Reactive board-change notifications (in-process hook — no new
        # job/queue/cron). Only meaningful once the source is on the
        # board (``accuracy`` non-None ⇔ >=1 resolved outcome ⇔ eligible
        # for the leaderboard). MUST be best-effort: a raise here would
        # propagate into ``_inline_recompute_with_async_fallback`` and
        # trigger a duplicate-snapshot re-enqueue (the snapshot is
        # already committed above), so we swallow + log everything.
        if accuracy is not None:
            try:
                await _emit_board_change_notifications(
                    session=session,
                    settings=settings,
                    source_id=source_id,
                    prev=prev,
                )
            except Exception:
                _log.exception(
                    "recompute_source_reputation: board-change hook failed for "
                    "source %s — rank-change DMs skipped this round",
                    source_id,
                )

        return pnl_pct


async def _emit_board_change_notifications(
    *,
    session: AsyncSession,
    settings: Settings,
    source_id: str,
    prev: SourceReputationRecord | None,
) -> None:
    """DM the handful of sources whose rank crossed a band (best-effort).

    Called AFTER the new snapshot is committed, so ``current_board_rows``
    is the *true* post-change board (it reflects this source's new row
    and any concurrent other-source recompute). ``pre`` is that same
    board with THIS source reverted to ``prev`` — or dropped if the
    source was cold-start (``prev is None`` / ``prev.accuracy_pct is
    None``). Because only this source's row differs between ``pre`` and
    ``post``, a concurrent other-source change sits identically in both
    and is never mis-attributed to this resolution; reported ranks are
    the true current ones.

    The classifier's candidate filter discards the deep field, so only
    the few sources that crossed the top-10 boundary or shuffled inside
    the top 3 get an identity lookup or a message. Only the opaque
    ``source_id`` is logged — never the raw Telegram id (MVP §15
    invariant 9). Sends are best-effort: ``TelegramNotifier`` swallows
    transport failures, and the caller wraps this whole function so a
    raise can never break the recompute.
    """
    from merlin_core.application.ports import BoardRow
    from merlin_core.application.use_cases.board_diff import classify, rank_board
    from merlin_infrastructure.db.repositories import (
        SourceIdentityRepository,
        SourceReputationRepository,
    )
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.notify.telegram.templates import render_board_change

    reputation = SourceReputationRepository(session)
    post_rows = await reputation.current_board_rows()

    if prev is None or prev.accuracy_pct is None:
        # Source was off the board before this resolution (first close):
        # ``pre`` is the post board without it.
        pre_rows = [row for row in post_rows if row.source_id != source_id]
    else:
        reverted = BoardRow(
            source_id=source_id,
            sort_score=prev.sort_score,
            outcomes_resolved=prev.outcomes_resolved,
            weighted_pnl_pct=prev.weighted_pnl_pct or 0.0,
        )
        pre_rows = [reverted if row.source_id == source_id else row for row in post_rows]

    notifications = classify(rank_board(pre_rows), rank_board(post_rows))
    if not notifications:
        return

    identities = SourceIdentityRepository(session)
    tg_by_source = await identities.get_tg_user_ids_by_source_ids(
        [n.source_id for n in notifications],
    )
    notifier = TelegramNotifier(bot_token=settings.telegram_bot_token or "noop")
    for notification in notifications:
        tg_user_id = tg_by_source.get(notification.source_id)
        if tg_user_id is None:
            _log.warning(
                "board-change: no identity row for source %s — skipping notification",
                notification.source_id,
            )
            continue
        text = render_board_change(
            notification.scenario,
            old_rank=notification.old_rank,
            new_rank=notification.new_rank,
        )
        await notifier.notify_user(tg_user_id, text)
        _log.info(
            "board-change: notified source %s scenario=%s (%s -> %s)",
            notification.source_id,
            notification.scenario.value,
            notification.old_rank,
            notification.new_rank,
        )
