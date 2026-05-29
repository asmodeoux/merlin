"""Unit tests for the horizon constants and helpers (MVP §15 invariant 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from merlin_core.domain.horizons import (
    HORIZON_HUMAN,
    HORIZON_SECONDS,
    horizon_human,
    horizon_to_timedelta,
)


def test_horizon_seconds_matches_mvp_invariant() -> None:
    assert HORIZON_SECONDS["24h"] == 86_400
    assert HORIZON_SECONDS["30d"] == 2_592_000
    assert HORIZON_SECONDS["90d"] == 7_776_000


def test_horizon_seconds_strictly_monotonic() -> None:
    codes = ["1h", "4h", "24h", "3d", "7d", "14d", "30d", "90d"]
    values = [HORIZON_SECONDS[c] for c in codes]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_horizon_seconds_no_dst_no_calendar() -> None:
    # 30d must be exactly 30 calendar days regardless of month, DST, etc.
    assert HORIZON_SECONDS["30d"] == 30 * 86_400
    assert HORIZON_SECONDS["7d"] == 7 * 86_400
    assert HORIZON_SECONDS["14d"] == 14 * 86_400


@pytest.mark.parametrize("code,expected", list(HORIZON_SECONDS.items()))
def test_horizon_to_timedelta_round_trip(code: str, expected: int) -> None:
    assert horizon_to_timedelta(code) == timedelta(seconds=expected)


def test_horizon_human_never_uses_codes() -> None:
    for code, label in HORIZON_HUMAN.items():
        assert code not in label, f"User-facing label for {code} must not contain the raw code"


def test_horizon_human_helper_returns_calendar_phrase() -> None:
    assert horizon_human("14d") == "2 weeks"
    assert horizon_human("24h") == "24 hours"
    assert horizon_human("90d") == "3 months"


def test_horizon_human_covers_every_code() -> None:
    assert set(HORIZON_HUMAN.keys()) == set(HORIZON_SECONDS.keys())
