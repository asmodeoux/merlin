"""Pure outcome-scoring helpers — one definition of "did the call hit?".

Two questions every consumer of an ``observed_return_pct`` needs to
answer, in exactly one place:

* **Was the move large enough to count?** ``is_flat`` — magnitude
  below the flat threshold counts as a wash regardless of sign.
* **Was the directional call correct?** ``is_correct_call`` — only
  True when the move cleared the flat threshold AND the sign matches
  the user's stated direction.

Two consumers today:

* The telegram outcome templates (won / lost / flat dispatch).
* The reputation recompute job (count "correct" rows for hit rate).

A single helper guarantees both views agree. The Sprint-1 regression
the user wants pinned in CI is "the notification said 'you called it
right' but the leaderboard didn't credit me" — that can only happen
if the two computations drift, and they only drift if they live in
two files.

User-locked v0 reputation choices (matching the conversation):

* ``flat_handling = wash_zero``: a flat outcome IS resolved but is NOT
  correct. Counts in the denominator, not the numerator.
* ``min_calls_for_rank = show_all``: there's no cold-start gate here
  — surfacing decisions live with the ``/board`` and ``/me`` readers.

This module is stdlib-only (domain rule) so it's importable from
``merlin_infrastructure`` (templates), ``merlin_worker`` (recompute),
and the API layer alike. The bot is not allowed to import
``merlin_core.domain`` — and doesn't need to, since the bot only
displays pre-computed outcomes.
"""

from __future__ import annotations

from typing import Final

# A move below this percentage counts as a wash for both notification
# framing and reputation scoring. 0.5% mirrors the telegram template's
# historical threshold; pinning it here as the single source means
# changing the bar in one place updates both views together.
FLAT_THRESHOLD_PCT: Final[float] = 0.5


def is_flat(observed_return_pct: float, *, flat_threshold_pct: float = FLAT_THRESHOLD_PCT) -> bool:
    """True iff ``|pct|`` is below the flat threshold (a wash).

    ``flat_threshold_pct`` is kw-only and exposed for future tuning;
    every caller in the v0 codebase passes the default. Override only
    when a test needs to probe edge cases at a different cutoff.
    """
    return abs(observed_return_pct) < flat_threshold_pct


def is_correct_call(
    direction: str,
    observed_return_pct: float,
    *,
    flat_threshold_pct: float = FLAT_THRESHOLD_PCT,
) -> bool:
    """True iff the user's directional call was correct AND not flat.

    Semantics (matches the telegram dispatch in ``render_outcome``):

    * ``|pct| < flat`` → False (wash counts as resolved but not correct).
    * ``direction == "long"`` → True iff ``pct > 0``.
    * ``direction == "short"`` → True iff ``pct < 0``.

    Unknown directions return False so a corrupted ``claims.direction``
    row never silently inflates a source's hit rate.
    """
    if is_flat(observed_return_pct, flat_threshold_pct=flat_threshold_pct):
        return False
    if direction == "long":
        return observed_return_pct > 0
    if direction == "short":
        return observed_return_pct < 0
    return False


def accuracy_pct(outcomes_correct: int, outcomes_resolved: int) -> float | None:
    """Plain hit rate as a percent. ``None`` if no outcomes resolved yet.

    Defining accuracy here (instead of inline in the resolver / reader)
    keeps the "what is reputation at v0" answer in one file alongside
    the correctness rule. Returns ``None`` rather than ``0.0`` on the
    cold-start case — readers (``/me``, ``/board``) need to tell
    "no history yet" from "a real 0% hit rate", and a float column
    can't carry that signal.
    """
    if outcomes_resolved <= 0:
        return None
    return 100.0 * outcomes_correct / outcomes_resolved


# ---------------------------------------------------------------------------
# Conviction-weighted PnL — Sprint-1.5 leaderboard refinement
# ---------------------------------------------------------------------------
#
# "Did the call hit?" (the boolean above) collapses a +0.6% squeaker and a
# +12% blow-out into one bit. The user-facing ``/board`` got more interesting
# (and more honest) when we added a magnitude axis: how much did the call
# move, in the user's favour, weighted by how confident they were when they
# placed it.
#
# Three layered functions, each testable in isolation:
#
#   conviction_weight(...)         per-bet stake multiplier from the
#                                  ``low / medium / high`` enum.
#   directional_return_pct(...)    signs ``observed_return_pct`` so positive
#                                  always means "the user was right".
#   weighted_mean_pnl_pct(...)     aggregate over a source's resolved bets.
#   shrunk_sort_score(...)         Bayesian-shrunk score used to RANK
#                                  sources on /board — hides the math from
#                                  the rendered row, drives the sort order.
#
# Locked v0 choices (matching the design conversation):
#
#   * weights = linear 1 / 3 / 5 / 7
#     (weak / medium / strong / very_strong) — the four-level enum the
#     bot already exposes. +2 step between tiers preserves the dynamics
#     reviewed in the worked examples while covering the full enum.
#   * aggregation = TRUE weighted mean: Σ(signed_return x weight) / Σ(weight).
#     Picked after the first live read of /board showed the alternative
#     (Σ(r x w) / n) inflating headline numbers beyond the underlying %
#     moves — a +7.6% TON bet with strong conviction rendered as +38%,
#     not +7.6%, which broke the "PnL = average % return" reading.
#     Weights still pull each row's magnitude AND tighten the divisor
#     when high-conviction bets dominate; the result lands in the range
#     of real % moves so "PnL" reads literally.
#   * flat threshold IS NOT applied to PnL — a +0.3% "flat" bet contributes
#     its honest signed value x weight, not 0 (matches the
#     ``flat_handling = honest_signed`` choice for PnL)
#   * shrinkage k = 5: half-strength at n=5 resolved bets, ~67% at n=10
#

#: Per-conviction stake multiplier. Linear 1/3/5/7 picked by the user
#: across the existing ``weak/medium/strong/very_strong`` enum. The +2
#: step between tiers keeps medium bets visible (3x a weak one) while
#: letting a very_strong bet drive 7x the impact — strong enough to
#: matter, not so dominant that one big-conviction call topples the
#: leaderboard alone (the Bayesian shrinkage + bet-count tiebreaker
#: handle that anyway).
CONVICTION_WEIGHTS: Final[dict[str, float]] = {
    "weak": 1.0,
    "medium": 3.0,
    "strong": 5.0,
    "very_strong": 7.0,
}

#: Bayesian-shrinkage pseudocount used by ``shrunk_sort_score``. n=k yields
#: a 50% confidence factor; smaller k rewards newcomers, larger k rewards
#: long track records. k=5 is the friendly middle for v0's small audience.
SORT_SCORE_K: Final[int] = 5


def conviction_weight(conviction: str) -> float:
    """Look up the per-bet stake multiplier for a conviction level.

    Unknown / missing conviction values fall back to the ``weak``
    weight (1.0) rather than raising — a corrupted ``claims.conviction``
    row should NOT crash a recompute, just under-credit that single
    bet. Default-to-weak (instead of medium or 0) keeps a malformed
    row from silently inflating a source's score.
    """
    return CONVICTION_WEIGHTS.get(conviction, CONVICTION_WEIGHTS["weak"])


def directional_return_pct(direction: str, observed_return_pct: float) -> float:
    """Sign the observed return so positive always means "call was right".

    * ``direction == "long"``  → return the value unchanged.
    * ``direction == "short"`` → flip the sign (a -2% move means the short
      paid +2%).
    * unknown direction → return ``0.0`` so a corrupted ``claims.direction``
      row contributes nothing to the source's PnL (same defensive posture
      as ``is_correct_call``).
    """
    if direction == "long":
        return observed_return_pct
    if direction == "short":
        return -observed_return_pct
    return 0.0


def weighted_mean_pnl_pct(
    contributions: list[float],
    weights: list[float],
) -> float | None:
    """True weighted mean of per-bet directional returns; ``None`` for cold-start.

    Formula::

        Σ(contributions) / Σ(weights)

    Where each ``contributions[i]`` is already ``directional_return_pct[i]
    x weights[i]`` (the per-bet weighted return), and ``weights[i]`` is
    the matching conviction weight. The result is the conviction-weighted
    average % return across a source's resolved bets — i.e. a number that
    LIVES in the range of the underlying % moves, so "PnL" reads
    literally on /board and /me.

    Worked example: BTC long weak (return -1.32%, weight 1) + TON short
    strong (directional return +7.61%, weight 5):

    * contributions: [-1.32 x 1, +7.61 x 5] = [-1.32, +38.07]
    * weights:       [1, 5]
    * Σ(contrib) / Σ(weight) = 36.75 / 6 = +6.13%

    Sits naturally between -1.32% and +7.61%, pulled toward the strong
    bet. Compare to the previous formula (Σ(r x w) / n), which produced
    +18.38% for the same inputs — a number above either bet's actual
    move.

    Conviction's role under this formula:

    * Mixed-conviction portfolios: high-conviction bets pull the
      average toward their results harder than low-conviction ones.
      That's where the signal lives.
    * Single-bet portfolios OR all-same-conviction portfolios:
      conviction drops out of the math. A solo +5% strong bet and a
      solo +5% weak bet both score +5%. This is intentional — with no
      conviction-mix to weigh, there's nothing to relate "strong" or
      "weak" against, so the honest % return wins.

    Raises ``ValueError`` if ``contributions`` and ``weights`` differ in
    length — those lists are produced in pairs by the worker recompute
    job and a drift between them would silently corrupt the score.

    Returns ``None`` rather than ``0.0`` on an empty list. Readers
    distinguish "no history" from "a real 0% PnL" — and the column on
    ``source_reputation_history`` is nullable for exactly this reason.
    Also returns ``None`` if Σ(weights) is 0 (impossible while
    ``conviction_weight`` floors at 1.0, but defensive against a
    future weight-tier rework that allowed a true zero).
    """
    if not contributions:
        return None
    if len(contributions) != len(weights):
        raise ValueError(
            f"contributions / weights length mismatch: "
            f"{len(contributions)} contributions vs {len(weights)} weights",
        )
    total_weight = sum(weights)
    if total_weight == 0:
        return None
    return sum(contributions) / total_weight


def shrunk_sort_score(weighted_pnl_pct: float | None, outcomes_resolved: int) -> float:
    """Bayesian-shrunk score used to RANK sources on /board.

    Formula::

        score = weighted_pnl_pct x n / (n + SORT_SCORE_K)

    Captures both concerns the user raised:

    * "One lucky bet shouldn't beat a consistent multi-bet user" — the
      ``n / (n + k)`` factor caps a single-bet score at ``1 / (1+k)`` of
      the raw PnL (16.7% at k=5), so a +10% home-run at n=1 reads as
      +1.67 rather than +10.
    * "Many low-PnL bets are spam" — low PnL x any factor stays low.
      A grinder at +0.3% never tops a disciplined caller at +5%.

    Cold-start sources (``weighted_pnl_pct is None`` OR
    ``outcomes_resolved <= 0``) return ``0.0``: they sort to the middle
    of the pack, dominated by anyone with positive PnL but ranking above
    sources with negative PnL. This avoids cold-start sources being
    silently boosted to ``+inf`` or pushed to ``-inf``, both of which
    would be product bugs.
    """
    if weighted_pnl_pct is None or outcomes_resolved <= 0:
        return 0.0
    n = outcomes_resolved
    return weighted_pnl_pct * n / (n + SORT_SCORE_K)
