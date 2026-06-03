"""``compute_ticker_breakdown`` — per-ticker view of a source's resolved calls.

Pure stdlib + domain scoring helpers. Lives in the application layer
because the input is a layer-crossing dataclass
(``ResolvedOutcomeForTicker``) — not raw SQL — and the output is the
shape the ``GET /me/tickers`` route hands to the wire DTO.

What this does in one sentence: group a source's resolved outcomes by
ticker, then run the same conviction-weighted PnL pipeline that
``recompute_source_reputation`` runs on the source as a whole, once
per ticker.

Reused scoring functions (zero math defined here):

* ``conviction_weight`` — per-bet stake multiplier.
* ``directional_return_pct`` — signs ``observed_return_pct`` so positive
  means the call was right.
* ``weighted_mean_pnl_pct`` — true conviction-weighted mean across
  the group.
* ``accuracy_pct`` — plain hit rate.
* ``shrunk_sort_score`` — Bayesian-shrunk PnL, drives the row order.

Sort order matches the leaderboard's tie-breakers:
``ORDER BY sort_score DESC, calls_resolved DESC, weighted_pnl_pct DESC``.
A single resolved call with a +20% PnL still loses to four resolved
calls averaging +6% because the shrinkage factor caps a solo bet at
``1 / (1 + k)`` of its raw PnL (16.7% at k=5). Same Bayesian discipline
as ``/board``, just sliced by ticker.

Cold-start tickers — ``calls_resolved == 0`` — never reach this module:
the repository's ``status='resolved'`` filter excludes them at the SQL
boundary, and an empty group never lands in the ``groupby``. So every
``TickerStats`` row this function emits has ``calls_resolved >= 1`` and
``accuracy_pct`` / ``weighted_pnl_pct`` non-None.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from merlin_core.application.ports import ResolvedOutcomeForTicker
from merlin_core.domain.scoring import (
    accuracy_pct as compute_accuracy_pct,
)
from merlin_core.domain.scoring import (
    conviction_weight,
    directional_return_pct,
    shrunk_sort_score,
    weighted_mean_pnl_pct,
)


@dataclass(frozen=True, slots=True)
class TickerStats:
    """One ticker's slice of a source's resolved-call portfolio.

    ``accuracy_pct`` and ``weighted_pnl_pct`` are non-Optional because
    every ``TickerStats`` row represents at least one resolved call
    (the function filters cold-start tickers out — empty groups never
    land here). Drop the Optional shape that ``MeResponse`` carries
    for the source-level view; per-ticker rows always have honest
    numbers.

    ``sort_score`` is internal — it drives the ORDER BY but is not
    rendered to users. The wire DTO ``TickerEntry`` drops the field
    (mirroring ``BoardEntry`` dropping the source-level sort score).
    """

    asset_symbol: str
    calls_resolved: int
    calls_correct: int
    accuracy_pct: float
    weighted_pnl_pct: float
    sort_score: float


def compute_ticker_breakdown(
    rows: list[ResolvedOutcomeForTicker],
) -> list[TickerStats]:
    """Group ``rows`` by ``asset_symbol`` and score each ticker.

    Empty input → empty output (cold-start source, no resolved
    outcomes at all).

    Per-ticker math is identical to the source-level recompute:
    contributions x weights, true weighted mean, Bayesian-shrunk
    sort score. Sort tie-breakers mirror the leaderboard:

    1. ``sort_score`` (descending) — primary rank.
    2. ``calls_resolved`` (descending) — between two equal-scored
       tickers, prefer the one backed by more bets (more confidence
       that the score is real).
    3. ``weighted_pnl_pct`` (descending) — final tiebreaker; on
       perfectly balanced inputs, the higher raw PnL wins.
    4. ``asset_symbol`` (ascending) — deterministic last-resort.
       Two tickers with identical n / pnl / sort_score sort
       alphabetically so the rendered order is stable across runs.

    ``weighted_mean_pnl_pct`` defends against the impossible-but-
    forward-compat ``None`` result (empty contributions or zero
    total weight) by falling back to ``0.0`` on the rendered row.
    The grouped paths can't hit this — ``conviction_weight`` floors
    at 1.0 and every group has at least one entry — but the cast
    keeps the type signature honest.
    """
    if not rows:
        return []

    grouped: dict[str, list[ResolvedOutcomeForTicker]] = defaultdict(list)
    for row in rows:
        grouped[row.asset_symbol].append(row)

    stats: list[TickerStats] = []
    for asset_symbol, group in grouped.items():
        n = len(group)
        correct = sum(1 for r in group if r.is_correct)
        accuracy = compute_accuracy_pct(correct, n)
        weights = [conviction_weight(r.conviction) for r in group]
        contributions = [
            directional_return_pct(r.direction, r.observed_return_pct) * w
            for r, w in zip(group, weights, strict=True)
        ]
        pnl = weighted_mean_pnl_pct(contributions, weights)
        sort = shrunk_sort_score(pnl, n)

        # Cold-start guard for the math layer's contract: ``accuracy``
        # / ``pnl`` are ``None`` only when ``n == 0``, which is
        # unreachable here (defaultdict groups always have >= 1 row).
        # Cast to satisfy the dataclass's non-Optional fields without
        # changing the runtime value for any realistic input.
        accuracy_value = accuracy if accuracy is not None else 0.0
        pnl_value = pnl if pnl is not None else 0.0

        stats.append(
            TickerStats(
                asset_symbol=asset_symbol,
                calls_resolved=n,
                calls_correct=correct,
                accuracy_pct=accuracy_value,
                weighted_pnl_pct=pnl_value,
                sort_score=sort,
            ),
        )

    stats.sort(
        key=lambda s: (-s.sort_score, -s.calls_resolved, -s.weighted_pnl_pct, s.asset_symbol),
    )
    return stats
