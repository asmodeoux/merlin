"""Unit tests for ``merlin_core.contracts.board``.

Pins the wire-level invariants of ``BoardResponse`` — specifically
the ``horizon`` echo field added for the ``/board`` Day Traders /
Swing Traders / All filter UI. Older callers + the v0 default both
land on ``"all"`` (no behavior change for unfiltered clients); only
``"all" / "short" / "long"`` are accepted on the wire so a malformed
or compromised client can't smuggle an unknown bucket through.
"""

from __future__ import annotations

import pytest
from merlin_core.contracts.board import (
    BoardEntry,
    BoardResponse,
    HorizonFilter,
)
from pydantic import ValidationError


def test_board_response_defaults_horizon_to_all() -> None:
    """Pre-filter clients still parse — ``horizon`` defaults to ``"all"``."""
    response = BoardResponse()
    assert response.horizon == "all"
    assert response.window == "all_time"
    assert response.entries == []


@pytest.mark.parametrize("horizon", ["all", "short", "long"])
def test_board_response_accepts_each_filter(horizon: HorizonFilter) -> None:
    """All three documented filter values parse cleanly."""
    response = BoardResponse(horizon=horizon)
    assert response.horizon == horizon


@pytest.mark.parametrize(
    "bad_horizon",
    ["medium", "intraday", "ALL", "", "long_term", "all-time", "24h"],
)
def test_board_response_rejects_unknown_filter(bad_horizon: str) -> None:
    """Anything outside the three-value Literal is a wire-level error.

    Future enums (e.g. a "Scalpers" tier) must land via an explicit
    schema bump + OpenAPI snapshot regen — they should NOT slip
    through a permissive parser today.
    """
    with pytest.raises(ValidationError):
        BoardResponse(horizon=bad_horizon)  # type: ignore[arg-type]


def test_board_response_preserves_entries_alongside_filter() -> None:
    """The new field is additive; the rest of the response shape is intact."""
    entry = BoardEntry(
        rank=1,
        display_name="Yuri",
        calls_resolved=4,
        calls_correct=3,
        accuracy_pct=75.0,
        weighted_pnl_pct=6.13,
    )
    response = BoardResponse(
        window="all_time",
        horizon="short",
        entries=[entry],
    )
    assert response.horizon == "short"
    assert len(response.entries) == 1
    assert response.entries[0].rank == 1
    assert response.entries[0].display_name == "Yuri"


def test_horizon_filter_literal_lists_exactly_three_values() -> None:
    """The Literal must mirror the wire constraint exactly.

    Prevents a sneaky widening — ``HorizonFilter`` is what the bot
    handler + API route both import; adding a fourth value should
    require an intentional touch here.
    """
    from typing import get_args

    assert set(get_args(HorizonFilter)) == {"all", "short", "long"}
