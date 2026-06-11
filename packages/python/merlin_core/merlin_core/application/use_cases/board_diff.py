"""Board-change classification — pure diff of two leaderboard snapshots.

The worker recomputes a source's reputation the moment a call closes
(Q-029, reactive, no cron). This module turns the *before* and *after*
leaderboards into the handful of "your rank moved" notifications worth
sending. Everything here is pure (stdlib + the layer-crossing
``BoardRow`` / ``RankedSource`` DTOs from ``application.ports``) so the
whole policy is unit-testable without a database or Telegram.

Two responsibilities:

* :func:`rank_board` — turn raw ``BoardRow``s into ranked positions
  using the SAME ordering key as the production ``/board`` query
  (``sort_score`` desc, ``outcomes_resolved`` desc, ``weighted_pnl_pct``
  desc) plus a ``source_id`` ascending tiebreaker. The tiebreaker makes
  ranking deterministic so an exact metric tie can't manufacture a
  phantom swap between the pre/post calls.

* :func:`classify` — diff the two ranked boards into ``BoardNotification``
  rows. A **candidate filter** runs first: any source whose rank stays
  outside the top-10 band on BOTH sides (e.g. #34 -> #38) is dropped
  before any classification, so we never compute or look up identities
  for the deep field. The elite movement band is the **top-3**.

Scope (locked in the plan §1/§5):

* ``entered_top10`` / ``fell_out_top10`` — top-10 boundary crossings.
  Gated: only emitted when the board has ``>= min_board_size_for_top10``
  eligible entries (on a tiny board being "top 10" is trivially true).
* ``took_throne`` / ``lost_throne`` — the #1 spot changed hands. NOT
  gated.
* ``climbed_top3`` / ``dropped_in_top3`` — a movement that touches the
  top 3 (old rank <= 3 or new rank <= 3). NOT gated. ``dropped_in_top3``
  also covers "dropped out of the top 3" (e.g. #3 -> #4).

Cold-start safety: a source with no resolved calls is filtered out of
the leaderboard upstream (``accuracy_pct IS NOT NULL``), so it appears
in neither ``pre`` nor ``post`` and can never be a target here. The
``climbed_top3`` / ``dropped_in_top3`` scenarios additionally require
BOTH ranks to be present (they describe movement *within* the ranked
set), so an entry from off-board never renders a "#None" message.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from merlin_core.application.ports import BoardRow, RankedSource

#: A source is only considered when its best (smallest) rank across the
#: pre/post boards is within this ceiling. Sources sitting outside the
#: top-10 band on both sides (the deep field) are dropped before any
#: classification — the efficiency cut the plan calls out.
CANDIDATE_RANK_CEILING: int = 10

#: Top-10 boundary used by ``entered_top10`` / ``fell_out_top10``.
TOP_10: int = 10

#: Elite movement band used by ``climbed_top3`` / ``dropped_in_top3``.
TOP_3: int = 3

#: The throne.
THRONE: int = 1

#: Default minimum eligible-board size before the top-10 boundary
#: messages are allowed to fire (cold-start guard).
DEFAULT_MIN_BOARD_SIZE_FOR_TOP10: int = 20


class BoardChangeScenario(StrEnum):
    """The six rank-change scenarios worth a proactive DM."""

    ENTERED_TOP10 = "entered_top10"
    FELL_OUT_TOP10 = "fell_out_top10"
    TOOK_THRONE = "took_throne"
    LOST_THRONE = "lost_throne"
    CLIMBED_TOP3 = "climbed_top3"
    DROPPED_IN_TOP3 = "dropped_in_top3"


@dataclass(frozen=True, slots=True)
class BoardNotification:
    """One source's classified rank change.

    ``old_rank`` / ``new_rank`` are 1-indexed, or ``None`` when the
    source was / is off the board. The worker maps ``scenario`` to a
    template and renders the ranks (templates only read the ranks the
    scenario guarantees are present — see the module docstring).
    """

    source_id: str
    scenario: BoardChangeScenario
    old_rank: int | None
    new_rank: int | None


def rank_board(rows: list[BoardRow]) -> list[RankedSource]:
    """Rank ``rows`` 1-indexed using the canonical ``/board`` order.

    Order: ``sort_score`` desc, ``outcomes_resolved`` desc,
    ``weighted_pnl_pct`` desc, then ``source_id`` asc as a deterministic
    last-resort tiebreaker. The tiebreaker is what lets the pre/post
    diff be stable: without it, two sources tied on every metric could
    enumerate in different orders between the two calls and fabricate a
    rank swap.
    """
    ordered = sorted(
        rows,
        key=lambda r: (
            -r.sort_score,
            -r.outcomes_resolved,
            -r.weighted_pnl_pct,
            r.source_id,
        ),
    )
    return [
        RankedSource(source_id=row.source_id, rank=rank)
        for rank, row in enumerate(ordered, start=1)
    ]


def classify(
    pre: list[RankedSource],
    post: list[RankedSource],
    *,
    min_board_size_for_top10: int = DEFAULT_MIN_BOARD_SIZE_FOR_TOP10,
) -> list[BoardNotification]:
    """Diff two ranked boards into the notifications worth sending.

    ``pre`` / ``post`` are the full eligible boards before and after one
    source's reputation change. ``board_size`` (for the top-10 gate) is
    ``len(post)``. Output is deterministic (sorted by ``source_id``).
    """
    pre_rank = {entry.source_id: entry.rank for entry in pre}
    post_rank = {entry.source_id: entry.rank for entry in post}
    board_size = len(post)

    notifications: list[BoardNotification] = []
    for source_id in sorted(pre_rank.keys() | post_rank.keys()):
        old_rank = pre_rank.get(source_id)
        new_rank = post_rank.get(source_id)
        scenario = _classify_one(
            old_rank,
            new_rank,
            board_size=board_size,
            min_board_size_for_top10=min_board_size_for_top10,
        )
        if scenario is not None:
            notifications.append(
                BoardNotification(
                    source_id=source_id,
                    scenario=scenario,
                    old_rank=old_rank,
                    new_rank=new_rank,
                ),
            )
    return notifications


def _classify_one(
    old_rank: int | None,
    new_rank: int | None,
    *,
    board_size: int,
    min_board_size_for_top10: int,
) -> BoardChangeScenario | None:
    """Classify a single source's move, or ``None`` if not worth a DM.

    Precedence (most specific first): throne -> top-10 boundary (gated)
    -> top-3 movement. The candidate filter short-circuits the deep
    field before any of that.
    """
    if old_rank == new_rank:
        return None

    old_eff = float(old_rank) if old_rank is not None else math.inf
    new_eff = float(new_rank) if new_rank is not None else math.inf

    # Candidate filter: ignore the deep field entirely.
    if min(old_eff, new_eff) > CANDIDATE_RANK_CEILING:
        return None

    return (
        _throne_change(old_rank, new_rank)
        or _top10_boundary_change(
            old_eff,
            new_eff,
            board_size=board_size,
            min_board_size=min_board_size_for_top10,
        )
        or _top3_movement(old_rank, new_rank)
    )


def _throne_change(old_rank: int | None, new_rank: int | None) -> BoardChangeScenario | None:
    """Rule 1 (never gated): the #1 spot changed hands."""
    if new_rank == THRONE and old_rank != THRONE:
        return BoardChangeScenario.TOOK_THRONE
    if old_rank == THRONE and new_rank is not None and new_rank != THRONE:
        return BoardChangeScenario.LOST_THRONE
    return None


def _top10_boundary_change(
    old_eff: float,
    new_eff: float,
    *,
    board_size: int,
    min_board_size: int,
) -> BoardChangeScenario | None:
    """Rule 2: top-10 boundary crossing — gated on a meaningful board."""
    if board_size < min_board_size:
        return None
    if old_eff > TOP_10 and new_eff <= TOP_10:
        return BoardChangeScenario.ENTERED_TOP10
    if old_eff <= TOP_10 and new_eff > TOP_10:
        return BoardChangeScenario.FELL_OUT_TOP10
    return None


def _top3_movement(old_rank: int | None, new_rank: int | None) -> BoardChangeScenario | None:
    """Rule 3: movement touching the top 3.

    Requires both ranks present — these describe movement *within* the
    ranked set, so an entry from / exit to off-board never renders a
    "#None" (those boundary cases are owned by rule 2). The caller has
    already excluded ``old_rank == new_rank``.
    """
    if old_rank is None or new_rank is None:
        return None
    if old_rank > TOP_3 and new_rank > TOP_3:
        return None
    if new_rank < old_rank:
        return BoardChangeScenario.CLIMBED_TOP3
    return BoardChangeScenario.DROPPED_IN_TOP3
