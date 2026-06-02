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
    FLAT_THRESHOLD_PCT,
    accuracy_pct,
    is_correct_call,
    is_flat,
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
