"""Unit tests for ``SourceReputationRepository``.

Strategy: hand-rolled session mock as in ``test_outcome_repository``.
The integration test exercises the actual leaderboard query against
Postgres; this file pins the write-path projection and the
cold-start behaviour of ``latest_for_source``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from merlin_infrastructure.db.models import SourceReputationHistory
from merlin_infrastructure.db.repositories.source_reputation_repository import (
    SourceReputationRepository,
)

pytestmark = pytest.mark.asyncio


def _fake_session() -> MagicMock:
    sess = MagicMock()
    sess.add = MagicMock()
    sess.flush = AsyncMock()
    sess.refresh = AsyncMock()
    sess.execute = AsyncMock()
    return sess


async def test_insert_snapshot_writes_every_field() -> None:
    """The history row must include the denormalised ``accuracy_pct``,
    ``weighted_pnl_pct``, and ``sort_score``."""
    sess = _fake_session()
    repo = SourceReputationRepository(sess)
    effective_at = datetime.now(tz=UTC)

    record = await repo.insert_snapshot(
        source_id="tg:" + "a" * 32,
        outcomes_resolved=4,
        outcomes_correct=3,
        accuracy_pct=75.0,
        weighted_pnl_pct=2.5,
        sort_score=1.111,
        triggering_outcome_id=None,
        effective_at=effective_at,
    )

    sess.add.assert_called_once()
    added = sess.add.call_args[0][0]
    assert isinstance(added, SourceReputationHistory)
    assert added.outcomes_resolved == 4
    assert added.outcomes_correct == 3
    assert added.accuracy_pct == pytest.approx(75.0)
    assert added.weighted_pnl_pct == pytest.approx(2.5)
    assert added.sort_score == pytest.approx(1.111)
    assert record.accuracy_pct == pytest.approx(75.0)
    assert record.weighted_pnl_pct == pytest.approx(2.5)
    assert record.sort_score == pytest.approx(1.111)


async def test_insert_snapshot_writes_null_accuracy_on_cold_start() -> None:
    """``outcomes_resolved=0`` MUST persist ``accuracy_pct=None`` AND
    ``weighted_pnl_pct=None``. ``sort_score`` stays ``0.0`` (NOT NULL).

    The CHECK constraints ``ck_srh_accuracy_null_iff_cold_start`` +
    ``ck_srh_pnl_null_iff_cold_start`` would reject anything else at
    the DB layer, but the application layer should also leave NULL by
    construction so a future migration that loosens the constraints
    doesn't quietly start persisting ``0.0`` (which the readers would
    misinterpret as a real hit rate / real PnL).
    """
    sess = _fake_session()
    repo = SourceReputationRepository(sess)

    record = await repo.insert_snapshot(
        source_id="tg:" + "b" * 32,
        outcomes_resolved=0,
        outcomes_correct=0,
        accuracy_pct=None,
        weighted_pnl_pct=None,
        sort_score=0.0,
        triggering_outcome_id=None,
        effective_at=datetime.now(tz=UTC),
    )

    added = sess.add.call_args[0][0]
    assert added.outcomes_resolved == 0
    assert added.accuracy_pct is None
    assert added.weighted_pnl_pct is None
    assert added.sort_score == 0.0
    assert record.accuracy_pct is None
    assert record.weighted_pnl_pct is None
    assert record.sort_score == 0.0


async def _scalar_returning(value: Any) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = value
    result.scalars.return_value = scalars
    return result


async def test_latest_for_source_cold_start_returns_none() -> None:
    """No snapshot rows → ``None``. Bot maps to "tracking your first
    resolutions"."""
    sess = _fake_session()
    sess.execute.return_value = await _scalar_returning(None)
    repo = SourceReputationRepository(sess)
    assert await repo.latest_for_source("tg:" + "c" * 32) is None


async def test_latest_for_source_projects_row_to_record() -> None:
    sess = _fake_session()
    row = MagicMock(spec=SourceReputationHistory)
    row.id = uuid4()
    row.source_id = "tg:" + "a" * 32
    row.outcomes_resolved = 10
    row.outcomes_correct = 7
    row.accuracy_pct = 70.0
    row.weighted_pnl_pct = 3.5
    row.sort_score = 2.333
    row.triggering_outcome_id = None
    row.effective_at = datetime.now(tz=UTC)
    sess.execute.return_value = await _scalar_returning(row)

    repo = SourceReputationRepository(sess)
    record = await repo.latest_for_source(row.source_id)

    assert record is not None
    assert record.outcomes_resolved == 10
    assert record.outcomes_correct == 7
    assert record.accuracy_pct == pytest.approx(70.0)
    assert record.weighted_pnl_pct == pytest.approx(3.5)
    assert record.sort_score == pytest.approx(2.333)


async def test_leaderboard_query_compiles_with_sort_score_order_by() -> None:
    """The leaderboard statement must compile (no typos in column refs)
    AND must ``ORDER BY sort_score DESC`` — the locked ranking key.

    We pin compile-time correctness; the actual ranking output is
    exercised in the integration test against a real DB. A regression
    here is the kind of "I renamed a column and broke the join" bug
    that doesn't show up until the first /board request — including
    the silent failure mode of falling back to accuracy_pct ordering.
    """
    sess = _fake_session()

    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time")

    assert entries == []
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    lowered = compiled.lower()
    assert "source_reputation_history" in lowered
    assert "accuracy_pct" in lowered  # still SELECTed for the row payload
    assert "weighted_pnl_pct" in lowered  # NEW: projected onto LeaderboardEntry
    assert "sort_score" in lowered  # NEW: the ORDER BY key
    assert "order by" in lowered
    # Pin that ``sort_score`` precedes ``outcomes_resolved`` in the
    # ORDER BY — if the order swaps we silently change the tiebreaker
    # semantics from "shrunk PnL, then volume" to "volume, then PnL".
    sort_score_pos = lowered.find("sort_score desc")
    outcomes_resolved_pos = lowered.find("outcomes_resolved desc")
    assert sort_score_pos >= 0, "sort_score is not ORDER BYed DESC"
    assert outcomes_resolved_pos >= 0, "outcomes_resolved tiebreaker missing"
    assert (
        sort_score_pos < outcomes_resolved_pos
    ), "sort_score must precede outcomes_resolved in the ORDER BY chain"


# ---------------------------------------------------------------------------
# Day Traders / Swing Traders horizon filter
# ---------------------------------------------------------------------------


def _joined_row(
    *,
    source_id: str,
    display_name: str,
    direction: str,
    conviction: str,
    observed_return_pct: float,
    is_correct: bool,
    resolved_at: datetime,
) -> MagicMock:
    """One row of the on-the-fly ``outcomes ⋈ claims ⋈ sources`` SELECT.

    Mirrors the seven columns the filtered ``leaderboard`` SELECT
    projects; ``MagicMock`` carries them as attributes so the
    repository's row-loop reads them via ``row.source_id`` etc.
    """
    row = MagicMock()
    row.source_id = source_id
    row.display_name = display_name
    row.direction = direction
    row.conviction = conviction
    row.observed_return_pct = observed_return_pct
    row.is_correct = is_correct
    row.resolved_at = resolved_at
    return row


@pytest.mark.parametrize("horizon", ["day", "swing", "position"])
async def test_leaderboard_filtered_emits_outcome_claim_join(horizon: str) -> None:
    """Filtered slices bypass the snapshot and join ``outcomes``+``claims``.

    Pins the structural choice — if a future refactor reads from
    ``source_reputation_history`` for the filtered slice too, the
    join will disappear and the snapshot-table-has-no-horizon-dim
    bug rides back in. Parametrised over the three filtered modes
    so each predicate-selector branch exercises the same shape.
    """
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon=horizon)

    assert entries == []
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    lowered = compiled.lower()
    assert "outcomes" in lowered
    assert "claims" in lowered
    assert "sources" in lowered
    assert "horizon_seconds" in lowered
    # Filtered path must NOT read from the snapshot table — that's
    # the whole point of the on-the-fly recompute.
    assert "source_reputation_history" not in lowered


async def test_leaderboard_day_only_includes_sub_24h_bets() -> None:
    """``horizon="day"`` aggregates only bets the SQL slice surfaces.

    We simulate the SQL filter by feeding the repo only rows the
    ``horizon_seconds <= 86_400`` predicate would have returned —
    one source's ≤24h bets — and assert (a) that source ranks #1,
    (b) the per-bet count matches the rows we fed, (c) the
    ``last_call_at`` is the max ``resolved_at`` of those rows.
    """
    resolved_at_1 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    resolved_at_2 = datetime(2026, 6, 2, 14, 30, tzinfo=UTC)
    rows = [
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name="DayTrader",
            direction="long",
            conviction="strong",
            observed_return_pct=4.0,
            is_correct=True,
            resolved_at=resolved_at_1,
        ),
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name="DayTrader",
            direction="short",
            conviction="medium",
            observed_return_pct=-2.0,
            is_correct=True,
            resolved_at=resolved_at_2,
        ),
    ]

    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="day")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.rank == 1
    assert entry.display_name == "DayTrader"
    assert entry.calls_resolved == 2
    assert entry.calls_correct == 2
    assert entry.accuracy_pct == pytest.approx(100.0)
    # last_call_at is the max resolved_at across the slice — the
    # filtered path is strictly more informative than the snapshot
    # path (which projects ``effective_at``).
    assert entry.last_call_at == resolved_at_2


async def test_leaderboard_swing_uses_bounded_predicate() -> None:
    """``horizon="swing"`` SELECT bounds horizon_seconds on BOTH sides.

    The swing bucket is the only one with a two-sided predicate (>
    day_max AND <= swing_max). Compiling the statement and pattern-
    matching the WHERE clause pins:

    * Both boundary constants appear as bind parameters — so a
      future rename of either constant fails this test before it
      reaches a live request.
    * The SQL references ``horizon_seconds`` at least twice
      (compiled output uses two parametrised comparisons).

    We deliberately don't assert the exact SQL string; SQLAlchemy
    compiles the same logical predicate in different surface forms
    across versions.
    """
    from merlin_core.contracts.horizons import (
        HORIZON_DAY_TRADER_MAX_SECONDS,
        HORIZON_SWING_TRADER_MAX_SECONDS,
    )

    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="swing")
    assert entries == []

    stmt = sess.execute.call_args[0][0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    rendered = str(compiled).lower()
    # Both boundary constants must appear in the WHERE clause —
    # ``literal_binds`` inlines them so we can pattern-match the
    # exact integers.
    assert str(HORIZON_DAY_TRADER_MAX_SECONDS) in rendered
    assert str(HORIZON_SWING_TRADER_MAX_SECONDS) in rendered
    # Two ``horizon_seconds`` references: one for the lower bound,
    # one for the upper bound.
    assert rendered.count("horizon_seconds") >= 2


async def test_leaderboard_swing_aggregates_bets_inside_the_window() -> None:
    """Fold-correctness for ``horizon="swing"`` on the bounded slice.

    Same shape as the day-bucket fold test: feed the rows the SQL
    slice would have returned (one source, two bets, both in the
    1d-3wk window) and assert the per-source aggregation lands
    cleanly.
    """
    resolved_at_1 = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
    resolved_at_2 = datetime(2026, 6, 8, 14, 30, tzinfo=UTC)
    rows = [
        _joined_row(
            source_id="tg:" + "b" * 32,
            display_name="SwingTrader",
            direction="long",
            conviction="strong",
            observed_return_pct=6.0,
            is_correct=True,
            resolved_at=resolved_at_1,
        ),
        _joined_row(
            source_id="tg:" + "b" * 32,
            display_name="SwingTrader",
            direction="long",
            conviction="medium",
            observed_return_pct=2.0,
            is_correct=True,
            resolved_at=resolved_at_2,
        ),
    ]
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="swing")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.display_name == "SwingTrader"
    assert entry.calls_resolved == 2
    assert entry.calls_correct == 2
    assert entry.last_call_at == resolved_at_2


async def test_leaderboard_position_returns_empty_at_v0() -> None:
    """At v0 the ``position`` bucket is expected to be empty.

    The user has zero >3wk resolutions today; the SQL slice returns
    nothing and the API returns ``entries=[]``. This is the load-
    bearing test for the bot's filter-aware empty-state copy on
    day one (the most-rendered empty-state path).

    Equivalent to the original "drops sources with no matching bets"
    behaviour — kept here as a position-bucket specific anchor.
    """
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="position")
    assert entries == []


async def test_leaderboard_horizon_ranks_by_shrunk_sort_score() -> None:
    """Two sources, same accuracy, different bet counts → shrinkage wins.

    Source A: one strong correct bet (+10% long, weight 5).
    Source B: ten strong correct bets (each +1% long, weight 5).

    Raw weighted PnL is +10% for A, +1% for B. Shrunk sort_score
    (k=5) caps A at 10 * 1 / (1+5) ≈ 1.67; B sits at
    1 * 10 / (10+5) ≈ 0.67. A should rank #1 — but if we ever
    ORDER BY raw PnL instead of shrunk sort score, A wins by
    +10 vs +1 (a much wider margin) which would be a regression we
    want to catch. Conversely the test pins that the ranking key
    is shrunk_sort_score, not raw weighted_pnl_pct.
    """
    resolved_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rows = []
    # Source A — one strong correct +10%
    rows.append(
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name="OneShot",
            direction="long",
            conviction="strong",
            observed_return_pct=10.0,
            is_correct=True,
            resolved_at=resolved_at,
        ),
    )
    # Source B — ten strong correct +1%
    for _ in range(10):
        rows.append(
            _joined_row(
                source_id="tg:" + "b" * 32,
                display_name="Grinder",
                direction="long",
                conviction="strong",
                observed_return_pct=1.0,
                is_correct=True,
                resolved_at=resolved_at,
            ),
        )

    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="day")

    assert [e.display_name for e in entries] == ["OneShot", "Grinder"]
    assert entries[0].rank == 1
    assert entries[1].rank == 2
    # Raw PnL is preserved for the row payload even though it's
    # not the ranking key — the bot renders this.
    assert entries[0].weighted_pnl_pct == pytest.approx(10.0)
    assert entries[1].weighted_pnl_pct == pytest.approx(1.0)


async def test_leaderboard_horizon_falls_back_to_anonymous_for_null_display_name() -> None:
    """``display_name IS NULL`` → ``"anonymous"``.

    Parity with the snapshot path. The bot relies on a non-empty
    name so the rendered row never has a blank slot.
    """
    rows = [
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name=None,  # type: ignore[arg-type]
            direction="long",
            conviction="weak",
            observed_return_pct=2.0,
            is_correct=True,
            resolved_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="day")
    assert entries[0].display_name == "anonymous"


async def test_leaderboard_all_projects_snapshot_rows_to_entries() -> None:
    """Snapshot path with a populated result projects each row 1:1.

    The original v0 test only fed an empty list, which compiled the
    SQL but never exercised the row projection. This test pins the
    populated branch: rank starts at 1, display_name falls back to
    'anonymous' on NULL, the four numeric fields land on the DTO,
    and ``last_call_at`` carries the snapshot's ``effective_at``
    (NOT the more-recent ``resolved_at`` — that's the horizon
    path's promise, not the snapshot path's).
    """
    eff_at = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    snapshot_a = MagicMock(spec=SourceReputationHistory)
    snapshot_a.outcomes_resolved = 10
    snapshot_a.outcomes_correct = 7
    snapshot_a.accuracy_pct = 70.0
    snapshot_a.weighted_pnl_pct = 4.5
    snapshot_a.effective_at = eff_at
    snapshot_b = MagicMock(spec=SourceReputationHistory)
    snapshot_b.outcomes_resolved = 3
    snapshot_b.outcomes_correct = 1
    snapshot_b.accuracy_pct = 33.3
    snapshot_b.weighted_pnl_pct = -1.2
    snapshot_b.effective_at = eff_at

    sess = _fake_session()
    result = MagicMock()
    # Two rows: the first source has a display name, the second is
    # NULL so we cover the ``or "anonymous"`` fallback at the same
    # time as the basic projection.
    result.all.return_value = [(snapshot_a, "Alice"), (snapshot_b, None)]
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time")

    assert len(entries) == 2
    assert entries[0].rank == 1
    assert entries[0].display_name == "Alice"
    assert entries[0].calls_resolved == 10
    assert entries[0].calls_correct == 7
    assert entries[0].accuracy_pct == pytest.approx(70.0)
    assert entries[0].weighted_pnl_pct == pytest.approx(4.5)
    assert entries[0].last_call_at == eff_at
    assert entries[1].rank == 2
    assert entries[1].display_name == "anonymous"


async def test_leaderboard_horizon_counts_incorrect_bets_against_accuracy() -> None:
    """A row with ``is_correct=False`` contributes to ``calls_resolved``
    but NOT to ``calls_correct`` — so accuracy is below 100%.

    Pins the False branch of the per-row ``if bool(row.is_correct):``
    guard. Without this test the branch is never exercised even
    though every row in earlier tests happens to be correct.
    """
    resolved_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rows = [
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name="MixedBag",
            direction="long",
            conviction="medium",
            observed_return_pct=4.0,
            is_correct=True,
            resolved_at=resolved_at,
        ),
        _joined_row(
            source_id="tg:" + "a" * 32,
            display_name="MixedBag",
            direction="long",
            conviction="medium",
            observed_return_pct=-3.0,
            is_correct=False,  # the branch under test
            resolved_at=resolved_at,
        ),
    ]
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="day")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.calls_resolved == 2
    assert entry.calls_correct == 1
    assert entry.accuracy_pct == pytest.approx(50.0)


async def test_leaderboard_position_uses_swing_max_as_lower_bound() -> None:
    """``horizon="position"`` exercises the ``> HORIZON_SWING_TRADER_MAX_SECONDS``
    branch of the predicate selector.

    Critical regression guard: a copy-paste bug could have the
    ``position`` branch reference the *day-trader* max instead of
    the swing-trader max, which would silently include swing bets
    in the position bucket. We compile the statement and look for
    the swing-max integer (and the absence of the day-max integer
    in a ``>`` context) to pin the right boundary.
    """
    from merlin_core.contracts.horizons import (
        HORIZON_DAY_TRADER_MAX_SECONDS,
        HORIZON_SWING_TRADER_MAX_SECONDS,
    )

    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = SourceReputationRepository(sess)
    entries = await repo.leaderboard("all_time", horizon="position")
    assert entries == []

    stmt = sess.execute.call_args[0][0]
    rendered = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    # Lower bound is the swing-trader max, NOT the day-trader max —
    # the copy-paste regression we're guarding against.
    assert str(HORIZON_SWING_TRADER_MAX_SECONDS) in rendered
    assert str(HORIZON_DAY_TRADER_MAX_SECONDS) not in rendered


@pytest.mark.parametrize(
    "bad_horizon",
    [
        # Legacy 2-bucket names — explicit regression guard so a
        # stale caller fails loudly at the adapter layer.
        "short",
        "long",
        # Generic misspellings.
        "medium",
        "intraday",
        "Day",
    ],
)
async def test_leaderboard_rejects_unknown_horizon_value(bad_horizon: str) -> None:
    """Unknown ``horizon`` raises rather than silently returning the
    unfiltered list.

    The route's regex gates the wire layer, but a direct application
    caller (worker job, future use case) gets a loud failure if it
    misspells the bucket name.
    """
    sess = _fake_session()
    repo = SourceReputationRepository(sess)
    with pytest.raises(ValueError, match="unknown horizon filter"):
        await repo.leaderboard("all_time", horizon=bad_horizon)
