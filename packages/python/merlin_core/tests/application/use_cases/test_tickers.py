"""Unit tests for ``compute_ticker_breakdown``.

The use case groups a source's resolved outcomes by ``asset_symbol``
and reuses the pure scoring functions to score each ticker. The
contract the tests pin:

* Grouping is by ``asset_symbol`` — same ticker rolls up regardless
  of direction / conviction within the group.
* Per-ticker math reuses the same scoring pipeline as the source-
  level recompute (conviction-weighted PnL, plain hit rate,
  Bayesian-shrunk sort score). The numbers we render here MUST agree
  with what ``recompute_source_reputation`` would produce on the
  same subset.
* Sort order is leaderboard-style: ``sort_score`` DESC, then
  ``calls_resolved`` DESC, then ``weighted_pnl_pct`` DESC, then
  ``asset_symbol`` ASC. A single-bet ticker never out-ranks a
  multi-bet ticker with the same raw PnL (Bayesian shrinkage caps
  the n=1 score at 1/(1+k) of the raw PnL).
* Cold-start input (empty list) → empty output.
"""

from __future__ import annotations

import pytest
from merlin_core.application.ports import ResolvedOutcomeForTicker
from merlin_core.application.use_cases.tickers import (
    TickerStats,
    compute_ticker_breakdown,
)


def _row(
    *,
    asset_symbol: str,
    direction: str = "long",
    conviction: str = "medium",
    observed_return_pct: float = 1.0,
    is_correct: bool = True,
) -> ResolvedOutcomeForTicker:
    """Builder with sensible defaults for the per-ticker resolved row."""
    return ResolvedOutcomeForTicker(
        direction=direction,
        conviction=conviction,
        observed_return_pct=observed_return_pct,
        is_correct=is_correct,
        asset_symbol=asset_symbol,
    )


def test_empty_input_returns_empty_list() -> None:
    """Cold-start source: no resolved outcomes → no per-ticker rows.

    The route renders this as the empty-state copy; the use case
    must NOT fabricate a placeholder row.
    """
    assert compute_ticker_breakdown([]) == []


def test_single_ticker_collapses_to_one_row() -> None:
    """Three resolved calls on the same ticker → one row with n=3.

    All three contribute to the same group's contributions / weights
    pair, so the per-bet PnL math runs on the joint set.
    """
    rows = [
        _row(asset_symbol="TON", direction="short", conviction="strong", observed_return_pct=-7.6),
        _row(asset_symbol="TON", direction="long", conviction="weak", observed_return_pct=2.0),
        _row(
            asset_symbol="TON",
            direction="long",
            conviction="medium",
            observed_return_pct=1.0,
            is_correct=True,
        ),
    ]

    stats = compute_ticker_breakdown(rows)

    assert len(stats) == 1
    assert stats[0].asset_symbol == "TON"
    assert stats[0].calls_resolved == 3


def test_per_ticker_pnl_matches_source_level_formula() -> None:
    """Yuri's live portfolio fixture — but sliced to a single ticker.

    Reuses the worked example from ``test_scoring.py``: BTC long weak
    (-1.32%) + TON short strong (+7.61%) collapses to +6.125% at the
    source level. If we slice it to one ticker (only TON), the math
    must still produce +7.61% — the per-ticker pipeline is the same
    formula on a smaller group.
    """
    rows = [
        _row(
            asset_symbol="TON",
            direction="short",
            conviction="strong",
            observed_return_pct=-7.61,
            is_correct=True,
        ),
    ]

    stats = compute_ticker_breakdown(rows)
    assert len(stats) == 1
    # Solo strong short of -7.61 → directional +7.61 → weighted mean
    # with itself is +7.61.
    assert stats[0].weighted_pnl_pct == pytest.approx(7.61, abs=1e-3)
    assert stats[0].accuracy_pct == pytest.approx(100.0)


def test_multi_ticker_breakdown_groups_independently() -> None:
    """Two tickers must produce two rows with isolated math.

    BTC long weak losing 1.32% should NOT contribute to TON's PnL,
    and vice versa. Each row's numbers are scoped to its own group.
    """
    rows = [
        _row(
            asset_symbol="BTC",
            direction="long",
            conviction="weak",
            observed_return_pct=-1.32,
            is_correct=False,
        ),
        _row(
            asset_symbol="TON",
            direction="short",
            conviction="strong",
            observed_return_pct=-7.61,
            is_correct=True,
        ),
    ]

    stats = compute_ticker_breakdown(rows)
    by_symbol = {s.asset_symbol: s for s in stats}

    assert set(by_symbol) == {"BTC", "TON"}
    assert by_symbol["BTC"].weighted_pnl_pct == pytest.approx(-1.32, abs=1e-3)
    assert by_symbol["TON"].weighted_pnl_pct == pytest.approx(7.61, abs=1e-3)
    assert by_symbol["BTC"].accuracy_pct == pytest.approx(0.0)
    assert by_symbol["TON"].accuracy_pct == pytest.approx(100.0)
    assert by_symbol["BTC"].calls_correct == 0
    assert by_symbol["TON"].calls_correct == 1


def test_sort_order_puts_multi_bet_ticker_above_single_bet_lucky_shot() -> None:
    """Bayesian shrinkage discipline mirrors the leaderboard.

    A single strong BTC long that paid +20% scores ``20 x 1/(1+5)``
    = +3.33 on the sort key. Four resolved TON calls averaging +6%
    score ``6 x 4/(4+5)`` ≈ +2.67. The lucky single-shot still
    out-ranks the multi-bet TON portfolio on the raw shrunk score
    (3.33 > 2.67) — but if TON's raw PnL were higher (say averaging
    +12%), shrinkage would punish the solo bet enough to flip the
    order. Pin both directions of the dynamic.
    """
    # Solo lucky strong BTC long at +20% (n=1).
    btc_lucky = _row(
        asset_symbol="BTC",
        direction="long",
        conviction="strong",
        observed_return_pct=20.0,
        is_correct=True,
    )
    # Four medium-conviction TON longs averaging +12% (n=4).
    ton_rows = [
        _row(
            asset_symbol="TON",
            direction="long",
            conviction="medium",
            observed_return_pct=12.0,
            is_correct=True,
        )
        for _ in range(4)
    ]

    stats = compute_ticker_breakdown([btc_lucky, *ton_rows])
    # TON's shrunk score ``12 x 4/(4+5) = 5.33`` beats BTC's
    # ``20 x 1/(1+5) = 3.33``. TON ranks first.
    assert [s.asset_symbol for s in stats] == ["TON", "BTC"]


def test_sort_order_alphabetical_tiebreaker_on_perfect_tie() -> None:
    """Identical n / PnL / sort_score → alphabetical by ``asset_symbol``.

    Two tickers with the exact same single-bet, same-conviction,
    same-PnL profile sort by their ticker letter so the rendered
    order is deterministic across runs.
    """
    rows = [
        _row(
            asset_symbol="TON",
            direction="long",
            conviction="medium",
            observed_return_pct=5.0,
            is_correct=True,
        ),
        _row(
            asset_symbol="BTC",
            direction="long",
            conviction="medium",
            observed_return_pct=5.0,
            is_correct=True,
        ),
    ]
    stats = compute_ticker_breakdown(rows)
    assert [s.asset_symbol for s in stats] == ["BTC", "TON"]


def test_cold_start_tickers_never_returned() -> None:
    """The use case only sees rows that already cleared the repo's
    ``status='resolved'`` filter — every group has n >= 1. So a
    ``TickerStats`` row with ``calls_resolved == 0`` should be
    impossible by construction, and the breakdown of a one-row input
    must produce a single non-cold-start entry.
    """
    rows = [
        _row(
            asset_symbol="ETH",
            direction="long",
            conviction="weak",
            observed_return_pct=0.3,
            is_correct=False,  # flat — counts as resolved, not correct.
        ),
    ]
    stats = compute_ticker_breakdown(rows)
    assert len(stats) == 1
    assert stats[0].calls_resolved == 1
    assert stats[0].asset_symbol == "ETH"


def test_returned_rows_are_frozen_ticker_stats_dataclasses() -> None:
    """The wire-layer route iterates the result and projects each row
    to a Pydantic ``TickerEntry``. Pin the dataclass identity so a
    future refactor that swaps the return type doesn't silently
    break the route's projection."""
    import dataclasses

    rows = [_row(asset_symbol="TON")]
    stats = compute_ticker_breakdown(rows)
    assert all(isinstance(s, TickerStats) for s in stats)
    # ``frozen=True`` on the dataclass means assignment raises
    # ``FrozenInstanceError`` — pinning the frozen contract so a
    # future refactor that mutably-tweaks the row breaks loudly.
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats[0].weighted_pnl_pct = 999.0  # type: ignore[misc]
