"""Unit tests for ``merlin_core.contracts.board``.

Pins the wire-level invariants of ``BoardResponse`` — specifically
the ``horizon`` echo field for the ``/board`` Day Traders / Swing
Traders / Position Traders / All filter UI. Older callers + the v0
default both land on ``"all"`` (no behavior change for unfiltered
clients); only ``"all" / "day" / "swing" / "position"`` are accepted
on the wire so a malformed or compromised client can't smuggle an
unknown bucket through. The renamed-away values ``"short"`` /
``"long"`` (from the original 2-bucket ship) are explicitly rejected
here as a regression guard.
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


@pytest.mark.parametrize("horizon", ["all", "day", "swing", "position"])
def test_board_response_accepts_each_filter(horizon: HorizonFilter) -> None:
    """All four documented filter values parse cleanly."""
    response = BoardResponse(horizon=horizon)
    assert response.horizon == horizon


@pytest.mark.parametrize(
    "bad_horizon",
    [
        # Legacy names from the original 2-bucket ship. Pinning them in
        # the reject set guards against a stale client whose hard-coded
        # callback_data outlasts the rename.
        "short",
        "long",
        # Generic typos / hostile values.
        "medium",
        "intraday",
        "ALL",
        "",
        "long_term",
        "all-time",
        "24h",
    ],
)
def test_board_response_rejects_unknown_filter(bad_horizon: str) -> None:
    """Anything outside the four-value Literal is a wire-level error.

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
        horizon="day",
        entries=[entry],
    )
    assert response.horizon == "day"
    assert len(response.entries) == 1
    assert response.entries[0].rank == 1
    assert response.entries[0].display_name == "Yuri"


def test_horizon_filter_literal_lists_exactly_four_values() -> None:
    """The Literal must mirror the wire constraint exactly.

    Prevents a sneaky widening — ``HorizonFilter`` is what the bot
    handler + API route both import; adding a fifth value should
    require an intentional touch here. Also pins that the renamed-
    away ``short`` / ``long`` keys did NOT leak back in.
    """
    from typing import get_args

    values = set(get_args(HorizonFilter))
    assert values == {"all", "day", "swing", "position"}
    assert "short" not in values
    assert "long" not in values


def test_board_response_defaults_open_activity_counts_to_zero() -> None:
    """``open_calls_total`` / ``open_callers_total`` default to ``0``.

    Pre-existing API clients (older bot deploys, third-party hooks)
    that never set these fields must parse the response cleanly —
    the cold-start payload is the realistic shape for the first
    minutes after deploy.
    """
    response = BoardResponse()
    assert response.open_calls_total == 0
    assert response.open_callers_total == 0


def test_board_response_accepts_open_activity_counts() -> None:
    """Both activity counts round-trip — the route writes them, the
    bot reads them. Pins the JSON field names exactly so a future
    renaming refactor (camelCase, etc.) fails here before reaching
    the cross-service wire.
    """
    response = BoardResponse(open_calls_total=22, open_callers_total=4)
    assert response.open_calls_total == 22
    assert response.open_callers_total == 4
    dumped = response.model_dump()
    assert dumped["open_calls_total"] == 22
    assert dumped["open_callers_total"] == 4


@pytest.mark.parametrize("field_name", ["open_calls_total", "open_callers_total"])
def test_board_response_rejects_negative_open_activity_counts(field_name: str) -> None:
    """Both counts are ``ge=0`` — a negative value is a server bug,
    not a wire shape.

    ``COUNT`` can never return negative, but the constraint also
    pins forward-compat: a future query rewrite that subtracts (e.g.
    "open minus soft-deleted") and underflows would surface here
    rather than silently rendering "-3 open calls" to users.
    """
    with pytest.raises(ValidationError):
        BoardResponse(**{field_name: -1})
