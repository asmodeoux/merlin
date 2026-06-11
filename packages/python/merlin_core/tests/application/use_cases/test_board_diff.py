"""Unit tests for the pure board-change classifier.

Contracts pinned here (plan §1/§5/§6):

* ``rank_board`` orders by the canonical ``/board`` key with a
  deterministic ``source_id`` tiebreaker and 1-indexes.
* ``classify`` emits the right scenario with the documented precedence
  (throne -> top-10 boundary -> top-3 movement), the candidate filter
  drops the deep field, the >=20 gate suppresses top-10 boundary
  messages on small boards, and a cold-start / off-board source never
  produces a "#None" render.
"""

from __future__ import annotations

import pytest
from merlin_core.application.ports import BoardRow, RankedSource
from merlin_core.application.use_cases.board_diff import (
    BoardChangeScenario,
    BoardNotification,
    classify,
    rank_board,
)


def _ranked(*source_ids: str) -> list[RankedSource]:
    """Build a ranked board straight from an ordered list of source ids."""
    return [RankedSource(source_id=sid, rank=i) for i, sid in enumerate(source_ids, start=1)]


def _by_source(notifications: list[BoardNotification]) -> dict[str, BoardNotification]:
    return {n.source_id: n for n in notifications}


# ---------------------------------------------------------------------------
# rank_board
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_rank_board_orders_by_canonical_key_and_one_indexes() -> None:
    rows = [
        BoardRow(source_id="b", sort_score=1.0, outcomes_resolved=5, weighted_pnl_pct=2.0),
        BoardRow(source_id="a", sort_score=3.0, outcomes_resolved=5, weighted_pnl_pct=2.0),
        BoardRow(source_id="c", sort_score=2.0, outcomes_resolved=5, weighted_pnl_pct=2.0),
    ]
    ranked = rank_board(rows)
    assert ranked == [
        RankedSource(source_id="a", rank=1),
        RankedSource(source_id="c", rank=2),
        RankedSource(source_id="b", rank=3),
    ]


@pytest.mark.fast
def test_rank_board_tiebreak_is_deterministic_by_source_id() -> None:
    # Every metric tied -> source_id ascending decides, deterministically.
    rows = [
        BoardRow(source_id="zeta", sort_score=1.0, outcomes_resolved=3, weighted_pnl_pct=1.0),
        BoardRow(source_id="alpha", sort_score=1.0, outcomes_resolved=3, weighted_pnl_pct=1.0),
    ]
    assert [r.source_id for r in rank_board(rows)] == ["alpha", "zeta"]
    # And re-ranking the same set (any input order) is stable.
    assert rank_board(rows) == rank_board(list(reversed(rows)))


@pytest.mark.fast
def test_rank_board_secondary_tiebreaks_resolved_then_pnl() -> None:
    rows = [
        BoardRow(source_id="x", sort_score=1.0, outcomes_resolved=2, weighted_pnl_pct=9.0),
        BoardRow(source_id="y", sort_score=1.0, outcomes_resolved=5, weighted_pnl_pct=1.0),
    ]
    # Same sort_score -> more resolved calls wins.
    assert [r.source_id for r in rank_board(rows)] == ["y", "x"]


# ---------------------------------------------------------------------------
# classify — top-10 boundary (gated)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_entered_top10_fires_on_large_board() -> None:
    pre = _ranked(*[f"s{i}" for i in range(1, 21)])  # 20 sources; target at #11
    # target "s11" climbs to #9; rebuild post with it moved up.
    post = _ranked(
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
        "s11",
        "s9",
        "s10",
        *[f"s{i}" for i in range(12, 21)],
    )
    result = _by_source(classify(pre, post))
    assert result["s11"].scenario is BoardChangeScenario.ENTERED_TOP10
    assert result["s11"].old_rank == 11
    assert result["s11"].new_rank == 9


@pytest.mark.fast
def test_fell_out_of_top10_fires_on_large_board() -> None:
    pre = _ranked(*[f"s{i}" for i in range(1, 21)])  # s10 at #10
    post = _ranked(
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
        "s9",
        "s11",
        "s10",
        *[f"s{i}" for i in range(12, 21)],
    )
    result = _by_source(classify(pre, post))
    assert result["s10"].scenario is BoardChangeScenario.FELL_OUT_TOP10
    assert result["s10"].old_rank == 10
    assert result["s10"].new_rank == 11


@pytest.mark.fast
def test_top10_boundary_suppressed_below_gate() -> None:
    # 19-source board: entering "top 10" is meaningless, suppress it.
    pre = _ranked(*[f"s{i}" for i in range(1, 20)])  # 19 sources
    post = _ranked(
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
        "s11",
        "s9",
        "s10",
        *[f"s{i}" for i in range(12, 20)],
    )
    # s11 moved 11 -> 9, but board_size 19 < 20: no entered_top10.
    assert all(n.source_id != "s11" for n in classify(pre, post))


@pytest.mark.fast
def test_gate_boundary_exactly_20_fires() -> None:
    pre = _ranked(*[f"s{i}" for i in range(1, 21)])  # exactly 20
    post = _ranked(
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
        "s11",
        "s9",
        "s10",
        *[f"s{i}" for i in range(12, 21)],
    )
    result = _by_source(classify(pre, post))
    assert result["s11"].scenario is BoardChangeScenario.ENTERED_TOP10


@pytest.mark.fast
def test_top3_and_throne_fire_regardless_of_board_size() -> None:
    # Tiny 4-source board: throne + top-3 movement still fire (not gated).
    pre = _ranked("a", "b", "c", "d")
    post = _ranked("b", "a", "c", "d")  # a 1->2, b 2->1
    result = _by_source(classify(pre, post))
    assert result["b"].scenario is BoardChangeScenario.TOOK_THRONE
    assert result["a"].scenario is BoardChangeScenario.LOST_THRONE


# ---------------------------------------------------------------------------
# classify — throne
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_took_throne_beats_climbed_top3_precedence() -> None:
    pre = _ranked("leader", "a", "b")
    post = _ranked("a", "leader", "b")  # a 2->1
    notifications = classify(pre, post)
    a = _by_source(notifications)["a"]
    assert a.scenario is BoardChangeScenario.TOOK_THRONE
    assert a.new_rank == 1
    # Exactly one notification for "a".
    assert sum(1 for n in notifications if n.source_id == "a") == 1


@pytest.mark.fast
def test_lost_throne_renders_new_rank() -> None:
    pre = _ranked("king", "a", "b")
    post = _ranked("a", "b", "king")  # king 1->3
    king = _by_source(classify(pre, post))["king"]
    assert king.scenario is BoardChangeScenario.LOST_THRONE
    assert king.old_rank == 1
    assert king.new_rank == 3


# ---------------------------------------------------------------------------
# classify — top-3 movement
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_climbed_within_top3() -> None:
    pre = _ranked("a", "b", "c", "d", "e")
    post = _ranked("a", "d", "b", "c", "e")  # d 4->2
    d = _by_source(classify(pre, post))["d"]
    assert d.scenario is BoardChangeScenario.CLIMBED_TOP3
    assert (d.old_rank, d.new_rank) == (4, 2)


@pytest.mark.fast
def test_dropped_out_of_top3_3_to_4_notifies() -> None:
    pre = _ranked("a", "b", "c", "d", "e")
    post = _ranked("a", "b", "d", "c", "e")  # c 3->4
    c = _by_source(classify(pre, post))["c"]
    assert c.scenario is BoardChangeScenario.DROPPED_IN_TOP3
    assert (c.old_rank, c.new_rank) == (3, 4)


@pytest.mark.fast
def test_4_to_5_fires_nothing() -> None:
    pre = _ranked("a", "b", "c", "d", "e", "f")
    post = _ranked("a", "b", "c", "e", "d", "f")  # d 4->5, e 5->4
    result = _by_source(classify(pre, post))
    assert "d" not in result
    assert "e" not in result


@pytest.mark.fast
def test_deep_field_move_dropped_by_candidate_filter() -> None:
    # 40 sources; a source goes #34 -> #38, others shuffle in the deep field.
    pre = _ranked(*[f"s{i}" for i in range(1, 41)])
    reordered = [f"s{i}" for i in range(1, 41)]
    # move s34 down to position 38
    reordered.remove("s34")
    reordered.insert(37, "s34")
    post = _ranked(*reordered)
    result = classify(pre, post)
    # s34 -> s38 stays outside the top-10 band on both sides, so the
    # candidate filter drops it before classification.
    assert all(n.source_id != "s34" for n in result)
    # And nobody else in the deep field is notified either.
    assert all(int(n.source_id[1:]) <= 10 for n in result)


# ---------------------------------------------------------------------------
# classify — displacement, entries, no-ops
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_displaced_user_is_notified() -> None:
    # X jumps from off-podium into #1, bumping the old #1->#2 and #2->#3.
    pre = _ranked("a", "b", "x", "c")
    post = _ranked("x", "a", "b", "c")  # x 3->1, a 1->2, b 2->3
    result = _by_source(classify(pre, post))
    assert result["x"].scenario is BoardChangeScenario.TOOK_THRONE
    assert result["a"].scenario is BoardChangeScenario.LOST_THRONE
    # b was displaced 2->3 purely by x's jump and is still notified.
    assert result["b"].scenario is BoardChangeScenario.DROPPED_IN_TOP3
    assert (result["b"].old_rank, result["b"].new_rank) == (2, 3)


@pytest.mark.fast
def test_brand_new_source_entering_top10_on_large_board() -> None:
    pre = _ranked(*[f"s{i}" for i in range(1, 21)])  # 20 incumbents
    # newcomer "new" enters at #7, everyone from 7 down shifts by one.
    post = _ranked(
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "new",
        *[f"s{i}" for i in range(7, 21)],
    )
    new = _by_source(classify(pre, post))["new"]
    assert new.scenario is BoardChangeScenario.ENTERED_TOP10
    assert new.old_rank is None
    assert new.new_rank == 7


@pytest.mark.fast
def test_entry_into_top3_on_small_board_does_not_render_none() -> None:
    # Small board (<20): top-10 gate off. A newcomer entering at #2 must
    # NOT classify as climbed_top3 (which would need an old rank) — it
    # either takes the throne (#1) or produces nothing here (#2).
    pre = _ranked("a", "b", "c")
    post = _ranked("a", "new", "b", "c")  # new enters at #2
    result = _by_source(classify(pre, post))
    if "new" in result:
        # If anything fires, it must not be a top3-movement scenario with
        # a missing old rank.
        assert result["new"].old_rank is not None
    # b dropped 2->3 (within top-3) IS a legitimate notification.
    assert result["b"].scenario is BoardChangeScenario.DROPPED_IN_TOP3


@pytest.mark.fast
def test_first_close_landing_at_45_sends_nothing() -> None:
    pre = _ranked(*[f"s{i}" for i in range(1, 45)])  # 44 incumbents
    post = _ranked(*[f"s{i}" for i in range(1, 45)], "newcomer")  # enters at #45
    assert all(n.source_id != "newcomer" for n in classify(pre, post))


@pytest.mark.fast
def test_no_change_emits_nothing() -> None:
    board = _ranked("a", "b", "c", "d")
    assert classify(board, board) == []
