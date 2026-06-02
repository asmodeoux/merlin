"""Unit tests for ``merlin_core.domain.scoring``.

The whole point of consolidating these helpers in one module is so the
notification template and the reputation recompute compute "correct"
the exact same way. Two suites pin the semantics:

1. Direction-specific truth tables — long-up wins, short-down wins,
   each side loses when the market goes against them.
2. Flat-threshold behaviour — magnitudes inside ``[-0.5, 0.5]`` are
   washes regardless of sign, and a wash counts as NOT correct
   (matches the v0 "wash_zero" choice the user picked).
"""

from __future__ import annotations

import pytest
from merlin_core.domain.scoring import (
    CONVICTION_WEIGHTS,
    FLAT_THRESHOLD_PCT,
    SORT_SCORE_K,
    accuracy_pct,
    conviction_weight,
    directional_return_pct,
    is_correct_call,
    is_flat,
    shrunk_sort_score,
    weighted_mean_pnl_pct,
)


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (0.0, True),
        (0.1, True),
        (-0.1, True),
        (0.4999, True),
        (-0.4999, True),
        (FLAT_THRESHOLD_PCT, False),
        (-FLAT_THRESHOLD_PCT, False),
        (1.0, False),
        (-1.0, False),
        (100.0, False),
    ],
)
def test_is_flat_uses_absolute_value_against_threshold(pct: float, expected: bool) -> None:
    """The threshold is symmetric. ``|pct| < 0.5`` → flat; else not."""
    assert is_flat(pct) is expected


def test_is_flat_threshold_override() -> None:
    """Override ``flat_threshold_pct`` for hypothetical sensitivity tests."""
    assert is_flat(0.6, flat_threshold_pct=1.0) is True
    assert is_flat(0.6, flat_threshold_pct=0.5) is False


@pytest.mark.parametrize(
    ("direction", "pct", "expected"),
    [
        # Long wins when the market goes up beyond the threshold.
        ("long", 5.0, True),
        ("long", 0.51, True),
        # Long loses when the market drops.
        ("long", -5.0, False),
        ("long", -0.51, False),
        # Long flat — wash counts as NOT correct (wash_zero choice).
        ("long", 0.0, False),
        ("long", 0.4, False),
        ("long", -0.4, False),
        # Short wins when the market drops.
        ("short", -5.0, True),
        ("short", -0.51, True),
        # Short loses when the market goes up.
        ("short", 5.0, False),
        ("short", 0.51, False),
        # Short flat — wash.
        ("short", 0.0, False),
        ("short", 0.3, False),
        # Unknown direction never credits the source.
        ("sideways", 5.0, False),
        ("", 5.0, False),
    ],
)
def test_is_correct_call_direction_truth_table(
    direction: str,
    pct: float,
    expected: bool,
) -> None:
    """Pin the won/lost/wash decision rule that both UI + reputation share."""
    assert is_correct_call(direction, pct) is expected


@pytest.mark.parametrize(
    ("correct", "resolved", "expected"),
    [
        (0, 0, None),
        (0, 1, 0.0),
        (1, 1, 100.0),
        (3, 4, 75.0),
        (7, 10, 70.0),
    ],
)
def test_accuracy_pct_uses_plain_hit_rate(
    correct: int,
    resolved: int,
    expected: float | None,
) -> None:
    """``accuracy_pct`` is ``100 * correct / resolved`` or ``None`` cold-start."""
    assert accuracy_pct(correct, resolved) == expected


def test_accuracy_pct_is_none_when_no_outcomes_resolved() -> None:
    """Cold-start signal — readers map ``None`` to "no data yet"."""
    assert accuracy_pct(0, 0) is None
    assert accuracy_pct(0, -1) is None  # defensive: negative is also "no data"


# ---------------------------------------------------------------------------
# Conviction weighting + PnL aggregation
# ---------------------------------------------------------------------------


def test_conviction_weights_are_linear_1_3_5_7() -> None:
    """Locked v0 spacing across the four-level enum: weak=1, medium=3,
    strong=5, very_strong=7. Changing these numbers silently re-orders
    the entire leaderboard, so pin them explicitly."""
    assert CONVICTION_WEIGHTS == {
        "weak": 1.0,
        "medium": 3.0,
        "strong": 5.0,
        "very_strong": 7.0,
    }


@pytest.mark.parametrize(
    ("conviction", "expected"),
    [
        ("weak", 1.0),
        ("medium", 3.0),
        ("strong", 5.0),
        ("very_strong", 7.0),
        # Defensive defaults — unknown values must not crash the recompute
        # and must not silently inflate a source's score.
        ("", 1.0),
        ("ultra", 1.0),
        ("WEAK", 1.0),  # case-sensitive on purpose; enum values are lowercase
        # ``low`` / ``high`` are obvious-but-wrong synonyms; we map them
        # to the conservative weak weight (not to medium/strong) to keep
        # the fallback predictable.
        ("low", 1.0),
        ("high", 1.0),
    ],
)
def test_conviction_weight_lookup_with_defaults(conviction: str, expected: float) -> None:
    """``conviction_weight`` is a pure dict lookup with a weak-weight fallback."""
    assert conviction_weight(conviction) == expected


@pytest.mark.parametrize(
    ("direction", "observed_pct", "expected"),
    [
        # Long: signed return passes through.
        ("long", 5.0, 5.0),
        ("long", -3.0, -3.0),
        ("long", 0.0, 0.0),
        # Short: sign flips. A -2% market move pays the short +2%.
        ("short", -2.0, 2.0),
        ("short", 4.0, -4.0),
        ("short", 0.0, 0.0),
        # Unknown direction contributes nothing — defensive, mirrors
        # is_correct_call's posture on malformed rows.
        ("sideways", 5.0, 0.0),
        ("", -10.0, 0.0),
    ],
)
def test_directional_return_pct_signs_by_direction(
    direction: str,
    observed_pct: float,
    expected: float,
) -> None:
    """``+`` always means "the user was right". Short of a corrupt row, every
    contribution comes from one of two branches that just sign the input."""
    assert directional_return_pct(direction, observed_pct) == expected


@pytest.mark.parametrize(
    ("contributions", "weights", "expected"),
    [
        # Cold-start — readers MUST distinguish from "real 0% PnL".
        ([], [], None),
        # Single bet, weak conviction: contribution / weight = bet's
        # honest signed return.
        ([5.0], [1.0], 5.0),
        ([-2.5], [1.0], -2.5),
        # Single bet, strong conviction: contribution is r x 5, weight
        # is 5 → result reduces to r. Solo bets are invariant to
        # conviction under true weighted mean — that's the design.
        ([25.0], [5.0], 5.0),  # +5% strong: 25/5 = +5
        ([-25.0], [5.0], -5.0),  # -5% strong: -25/5 = -5
        # All-same-conviction portfolios collapse to mean(signed_returns).
        # (1+3+5)/(1+1+1) = 9/3 = 3.0 — same as the un-weighted mean.
        ([1.0, 3.0, 5.0], [1.0, 1.0, 1.0], 3.0),
        # Mixed conviction — strong bets pull harder. (25+5)/(5+1) =
        # 30/6 = +5.0 — the strong +5% bet drags the weak +5% bet
        # toward its result (which is the same here, so result = +5).
        ([25.0, 5.0], [5.0, 1.0], 5.0),
        # Mixed conviction, mixed signs (the Yuri TON+BTC live case):
        # (-1.32 + 38.07) / (1 + 5) = 36.75 / 6 = +6.125
        ([-1.32, 38.07], [1.0, 5.0], pytest.approx(6.125)),
        # The cross-check that motivated the move away from Σ/n:
        # under THIS formula a strong loss + a weak win nets negative
        # (the strong loss pulls harder).
        # contributions: [-25, +5], weights: [5, 1]
        # → -20 / 6 ≈ -3.33
        ([-25.0, 5.0], [5.0, 1.0], pytest.approx(-20.0 / 6.0)),
    ],
)
def test_weighted_mean_pnl_pct_is_true_weighted_mean(
    contributions: list[float],
    weights: list[float],
    expected: float | None,
) -> None:
    """The locked aggregation is ``Σ(contributions) / Σ(weights)`` — a
    TRUE weighted mean. The result sits in the range of underlying %
    moves so "PnL" reads literally on /board / /me.

    History: the previous formula was ``Σ(contributions) / n``, which
    inflated single high-conviction bets by up to 7x and made the
    headline number scale with conviction multipliers rather than
    actual % returns. The switch happened after the first live read
    of /board produced +18.38% for a portfolio whose underlying
    moves were -1.3% and +7.6%.
    """
    if expected is None:
        assert weighted_mean_pnl_pct(contributions, weights) is None
    else:
        assert weighted_mean_pnl_pct(contributions, weights) == expected


def test_weighted_mean_pnl_pct_raises_on_length_mismatch() -> None:
    """``contributions`` and ``weights`` are produced as a pair by the
    worker. A length drift would silently corrupt the score — we
    raise loudly instead of "best-effort" zip + truncate."""
    with pytest.raises(ValueError, match="length mismatch"):
        weighted_mean_pnl_pct([1.0, 2.0], [1.0])
    with pytest.raises(ValueError, match="length mismatch"):
        weighted_mean_pnl_pct([1.0], [1.0, 2.0])


def test_weighted_mean_pnl_pct_returns_none_when_total_weight_is_zero() -> None:
    """``conviction_weight`` floors at 1.0 today so Σw cannot be 0 in
    practice — but if a future weight-tier rework allowed a true
    zero weight (e.g. an "experimental" tier that doesn't count),
    Σw could hit 0. We return ``None`` rather than divide-by-zero.
    """
    assert weighted_mean_pnl_pct([0.0, 0.0], [0.0, 0.0]) is None


def test_weighted_mean_pnl_pct_end_to_end_through_helpers() -> None:
    """End-to-end: build contributions + weights via the public helpers
    + take the true weighted mean. Pins the contract that the recompute
    job follows exactly."""
    rows = [
        # (direction, observed_pct, conviction, conviction_weight, contribution)
        ("long", 5.0, "strong"),  # w=5, r=+5, contribution=+25
        ("short", 3.0, "weak"),  # w=1, r=-3, contribution=-3
        ("long", -1.3, "medium"),  # w=3, r=-1.3, contribution=-3.9
        ("long", 2.0, "very_strong"),  # w=7, r=+2, contribution=+14
    ]
    weights = [conviction_weight(c) for _, _, c in rows]
    contributions = [
        directional_return_pct(d, p) * w for (d, p, _), w in zip(rows, weights, strict=True)
    ]
    assert weights == [5.0, 1.0, 3.0, 7.0]
    assert contributions == pytest.approx([25.0, -3.0, -3.9, 14.0])
    mean = weighted_mean_pnl_pct(contributions, weights)
    assert mean is not None
    # True weighted mean: Σcontrib / Σw = (25 - 3 - 3.9 + 14) / 16
    #                                   = 32.1 / 16 = +2.00625
    assert mean == pytest.approx((25.0 - 3.0 - 3.9 + 14.0) / (5.0 + 1.0 + 3.0 + 7.0))
    assert mean == pytest.approx(2.00625)


# ---------------------------------------------------------------------------
# Conviction-mix invariants — the behavioural promises the formula makes
# ---------------------------------------------------------------------------


def test_all_same_conviction_collapses_to_unweighted_mean() -> None:
    """When every bet shares a conviction, the weights cancel out of
    Σ(r*w)/Σ(w) and the result reduces to the plain mean of signed
    returns. This is the "conviction is relative within a portfolio"
    property the formula encodes.

    Two users with identical outcomes but opposite uniform conviction
    levels MUST score the same — neither was differentiating their
    confidence between bets, so the score is just their hit-rate-x-
    magnitude average.
    """
    returns = [5.0, 3.0, 1.0]  # three winning bets, varying magnitude

    all_weak = weighted_mean_pnl_pct(
        contributions=[r * 1.0 for r in returns],
        weights=[1.0] * len(returns),
    )
    all_strong = weighted_mean_pnl_pct(
        contributions=[r * 5.0 for r in returns],
        weights=[5.0] * len(returns),
    )
    all_very_strong = weighted_mean_pnl_pct(
        contributions=[r * 7.0 for r in returns],
        weights=[7.0] * len(returns),
    )

    assert all_weak == pytest.approx(3.0)
    assert all_strong == pytest.approx(3.0)
    assert all_very_strong == pytest.approx(3.0)


def test_mixed_conviction_pulls_average_toward_higher_weight_bets() -> None:
    """Two-bet portfolio: +5% strong + -5% weak. Strong bet (weight 5)
    pulls harder than the weak loss (weight 1), so the average should
    be positive — calibration is rewarded.

    Counter-case: -5% strong + +5% weak. Now the loss has the higher
    weight, so the average lands negative — uncalibrated big-conviction
    bets are punished.
    """
    # Calibrated: strong-conviction win + weak-conviction loss.
    calibrated = weighted_mean_pnl_pct(
        contributions=[25.0, -5.0],  # +5*5, -5*1
        weights=[5.0, 1.0],
    )
    # (25 - 5) / 6 = +3.33
    assert calibrated == pytest.approx(20.0 / 6.0)
    assert calibrated is not None
    assert calibrated > 0

    # Uncalibrated: strong-conviction loss + weak-conviction win.
    uncalibrated = weighted_mean_pnl_pct(
        contributions=[-25.0, 5.0],  # -5*5, +5*1
        weights=[5.0, 1.0],
    )
    # (-25 + 5) / 6 = -3.33
    assert uncalibrated == pytest.approx(-20.0 / 6.0)
    assert uncalibrated is not None
    assert uncalibrated < 0


def test_yuri_live_portfolio_renders_in_range_of_underlying_moves() -> None:
    """The portfolio that motivated this formula change: a weak-
    conviction BTC long that lost 1.32%, plus a strong-conviction
    TON short that paid +7.61% directional.

    Under the previous Σ/n formula this rendered as +18.38% — above
    either bet's actual move. Under true weighted mean it renders
    as +6.125%, sitting naturally between -1.32% and +7.61%.

    The regression we never want to repeat is exactly this one — pin
    the live value as a contract test so any future formula tweak
    fails loudly here.
    """
    pnl = weighted_mean_pnl_pct(
        # BTC long weak: -1.32 x 1 = -1.32
        # TON short strong: +7.61 x 5 = +38.05
        contributions=[-1.32, 38.05],
        weights=[1.0, 5.0],
    )
    assert pnl is not None
    assert pnl == pytest.approx(6.121666666, abs=1e-3)
    # Sits inside the range of underlying directional moves.
    assert -1.32 < pnl < 7.61


# ---------------------------------------------------------------------------
# Bayesian shrinkage — the sort key
# ---------------------------------------------------------------------------


def test_sort_score_k_is_locked_at_five() -> None:
    """k=5 is the friendly middle the user picked for v0. Half-strength
    at n=5, ~67% at n=10. Pin so a refactor can't silently change the
    leaderboard dynamics."""
    assert SORT_SCORE_K == 5


@pytest.mark.parametrize(
    ("pnl", "n", "expected_score"),
    [
        # Single home-run bet — shrinkage demotes hard.
        # 10 x 1 / 6 = 1.666...
        (10.0, 1, 10.0 / 6),
        # Half-strength at n = k.
        # 4 x 5 / 10 = 2.0
        (4.0, 5, 2.0),
        # Saturating: large n approaches the raw PnL.
        # 2 x 30 / 35 ≈ 1.714
        (2.0, 30, 2.0 * 30 / 35),
        # Negative PnL x large n → strongly negative score (active loser).
        (-2.0, 20, -2.0 * 20 / 25),
        # Spam: low PnL x any n stays near zero.
        (0.3, 50, 0.3 * 50 / 55),
    ],
)
def test_shrunk_sort_score_formula(
    pnl: float,
    n: int,
    expected_score: float,
) -> None:
    """Pin the exact arithmetic of ``pnl x n / (n + k)`` against worked
    examples from the design conversation."""
    assert shrunk_sort_score(pnl, n) == pytest.approx(expected_score)


def test_shrunk_sort_score_cold_start_is_zero_not_negative_infinity() -> None:
    """A source with no resolved bets must score 0.0, sorting to the middle
    of the pack. Earlier draft used ``float('-inf')`` which buried cold-
    start sources beneath active losers — a product bug."""
    assert shrunk_sort_score(None, 0) == 0.0
    assert shrunk_sort_score(None, 5) == 0.0  # impossible (None implies 0) but
    # defensive
    assert shrunk_sort_score(5.0, 0) == 0.0


def test_shrunk_sort_score_orders_consistent_grinder_below_disciplined_caller() -> None:
    """The design promise: "+5% / n=8" beats "+2% / n=30" beats a single
    "+10% / n=1" home-run. Pinning the ordering here catches any future
    refactor that quietly changes the shape of the curve."""
    home_run = shrunk_sort_score(10.0, 1)  # ≈ 1.67
    grinder = shrunk_sort_score(2.0, 30)  # ≈ 1.71
    disciplined = shrunk_sort_score(5.0, 8)  # ≈ 3.08
    spammer = shrunk_sort_score(0.3, 50)  # ≈ 0.27
    loser = shrunk_sort_score(-2.0, 20)  # ≈ -1.6

    assert disciplined > grinder > home_run > spammer > loser
