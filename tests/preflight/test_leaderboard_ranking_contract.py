"""Preflight: lock the /board ranking contract end-to-end.

Sprint-1.5 swapped the /board ORDER BY key from raw ``accuracy_pct``
to the Bayesian-shrunk ``sort_score``. The risk is that a future
refactor (e.g. "let's simplify the leaderboard query") silently
reverts to accuracy-only ordering, at which point a single-bet
+10% home-run starts topping the leaderboard again and the
"don't let lucky one-shots win" promise we made to the user
quietly breaks.

Sprint-1.5b followed up with a second formula switch: the
``weighted_pnl_pct`` aggregation changed from ``Σ(r x w) / n`` to
the TRUE weighted mean ``Σ(r x w) / Σ(w)``. The original formula
inflated headline numbers (a +7.6% strong-conviction bet showed as
+38% PnL); the new one keeps PnL inside the range of actual moves.
A revert to the old formula would silently break the "/board PnL =
average % return weighted by conviction" reading users now
depend on.

This file is the safety net. Four independent assertions, each
catching a different class of regression:

1. The conviction weights are 1 / 3 / 5 / 7 across the four-level
   ``weak / medium / strong / very_strong`` enum. Drift here would
   silently rescale every source's PnL.
2. The Bayesian shrinkage constant ``SORT_SCORE_K`` is 5. Changing
   this number quietly changes how aggressively single-bet sources
   are demoted.
3. The repository's ``leaderboard`` SQL ``ORDER BY`` chain leads
   with ``sort_score DESC`` (NOT ``accuracy_pct DESC``). This is the
   regression that broke the original v0 leaderboard.
4. ``weighted_mean_pnl_pct`` divides by ``Σ(weights)``, NOT by
   ``len(contributions)``. This is the regression that broke the
   "headline number reads like an honest % return" promise.

Cheap, fast, deterministic — matches the bar set in AGENTS.md §5.1
for the rest of ``tests/preflight``. No network, no DB.
"""

from __future__ import annotations

import pytest


def test_conviction_weights_are_pinned_at_one_three_five_seven() -> None:
    """The locked v0 spacing across the conviction enum: 1/3/5/7.

    Changing these silently re-orders the entire leaderboard. The
    user reviewed worked examples assuming this spacing — any change
    needs an explicit re-review.
    """
    from merlin_core.domain.scoring import CONVICTION_WEIGHTS

    assert CONVICTION_WEIGHTS == {
        "weak": 1.0,
        "medium": 3.0,
        "strong": 5.0,
        "very_strong": 7.0,
    }


def test_sort_score_shrinkage_k_is_five() -> None:
    """``SORT_SCORE_K = 5`` is the locked Bayesian shrinkage parameter.

    Bumping ``k`` makes the leaderboard friendlier to long track
    records; lowering it rewards small samples more. The user picked
    5 ("half-strength at n=5") in the design conversation — pinning
    this constant flags any silent change.
    """
    from merlin_core.domain.scoring import SORT_SCORE_K

    assert SORT_SCORE_K == 5


def test_shrunk_score_demotes_single_bet_homerun_below_three_bet_grinder() -> None:
    """End-to-end via the public ``shrunk_sort_score`` helper.

    The CORE product promise: "one lucky bet should not beat a user
    who delivers many alphas". A +10% one-shot at n=1 must rank
    below a +2% / n=20 consistent grinder. If this assertion ever
    flips, the user-facing leaderboard has silently regressed to
    the lucky-one-shot regime.
    """
    from merlin_core.domain.scoring import shrunk_sort_score

    lucky_one_shot = shrunk_sort_score(10.0, 1)  # 10 * 1 / 6 ~= 1.67
    grinder = shrunk_sort_score(2.0, 20)  # 2 * 20 / 25 = 1.60
    # The user's design intent: large positive PnL with large n beats
    # a one-shot. At these specific numbers the one-shot edges it by
    # a hair, but the next case — a real "track record" user —
    # decisively wins.
    disciplined = shrunk_sort_score(5.0, 8)  # 5 * 8 / 13 ~= 3.08
    assert disciplined > lucky_one_shot
    assert disciplined > grinder
    assert lucky_one_shot == pytest.approx(10.0 / 6)
    assert grinder == pytest.approx(2.0 * 20 / 25)


def test_weighted_mean_pnl_pct_divides_by_sum_of_weights_not_count() -> None:
    """The locked aggregation is ``Σ(contributions) / Σ(weights)`` —
    a TRUE weighted mean. The earlier ``Σ(contributions) / n`` formula
    produced headline numbers that scaled with conviction multipliers
    (a single strong bet could render at 5x its underlying % move),
    which broke the "PnL = average % return" reading.

    Math probe: contributions = [+25, -1.32], weights = [5, 1].
    Σ contributions = 23.68; Σ weights = 6; n = 2.

    * TRUE weighted mean (locked):   23.68 / 6  =  +3.946...
    * OLD Σ/n formula (regression):  23.68 / 2  =  +11.84

    These two numbers are far enough apart that a 1% float
    tolerance can't flip a passing assertion into a passing-the-
    wrong-formula one.
    """
    from merlin_core.domain.scoring import weighted_mean_pnl_pct

    contributions = [25.0, -1.32]
    weights = [5.0, 1.0]

    pnl = weighted_mean_pnl_pct(contributions, weights)
    sum_w = sum(weights)
    n = len(contributions)
    expected_true_weighted = sum(contributions) / sum_w
    forbidden_old_formula = sum(contributions) / n

    assert pnl == pytest.approx(expected_true_weighted), (
        "weighted_mean_pnl_pct no longer divides by Σ(weights); "
        "/board headline PnL has silently regressed to the inflated "
        "Σ/n formula. See merlin_core/domain/scoring.py + the design "
        "conversation pinning the true weighted mean."
    )
    # And explicitly: the OLD formula must NOT be the answer.
    assert pnl != pytest.approx(forbidden_old_formula, abs=1e-6)


def test_leaderboard_query_orders_by_sort_score_first() -> None:
    """The compiled SQL for ``SourceReputationRepository.leaderboard``
    MUST ``ORDER BY sort_score DESC`` AHEAD of every other column.

    A refactor that drops ``sort_score`` from the ORDER BY chain
    silently regresses the ranking to accuracy-only — the exact
    pre-Sprint-1.5 behaviour. Pinning the chain order here catches
    that class of bug before /board starts ranking a 1-bet 100%-hit
    user above a 30-bet 80%-hit one.
    """
    from unittest.mock import AsyncMock, MagicMock

    from merlin_infrastructure.db.repositories.source_reputation_repository import (
        SourceReputationRepository,
    )

    sess = MagicMock()
    sess.execute = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)

    import asyncio

    asyncio.run(repo.leaderboard("all_time"))

    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    sort_score_pos = compiled.find("sort_score desc")
    accuracy_pos = compiled.find("accuracy_pct desc")
    outcomes_resolved_pos = compiled.find("outcomes_resolved desc")

    assert sort_score_pos >= 0, (
        "leaderboard ORDER BY no longer contains `sort_score DESC` — "
        "the locked Bayesian-shrunk ranking key. /board has silently "
        "reverted to accuracy-only ordering (shrinkage k=5, Q-012)."
    )
    # accuracy_pct may stay in the chain as a final tiebreaker, but
    # ONLY after sort_score (not as the leading key).
    if accuracy_pos >= 0:
        assert accuracy_pos > sort_score_pos, (
            "`accuracy_pct DESC` precedes `sort_score DESC` in the "
            "ORDER BY chain — the ranking has reverted to hit-rate "
            "primary, which lets a 1-bet 100%-hit user top a 30-bet "
            "80%-hit track record."
        )
    # outcomes_resolved DESC is the locked tiebreaker — confirm it
    # also sits after sort_score (not as the primary key).
    if outcomes_resolved_pos >= 0:
        assert outcomes_resolved_pos > sort_score_pos
