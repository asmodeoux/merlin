"""Unit tests for ``OutcomeRepository`` — append-only write path + reads.

Strategy mirrors ``test_claim_repository``: hand-rolled
``AsyncMock``-backed session captures the ``add`` / ``flush`` /
``refresh`` calls so we can assert what gets persisted without
spinning up Postgres.

The integration test (``tests/integration``) exercises the actual
DB-side append-only triggers; this file pins the application-side
shape of the writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from merlin_infrastructure.db.models import Outcome
from merlin_infrastructure.db.repositories.outcome_repository import OutcomeRepository

pytestmark = pytest.mark.asyncio


def _fake_session() -> MagicMock:
    sess = MagicMock()
    sess.add = MagicMock()
    sess.flush = AsyncMock()
    sess.refresh = AsyncMock()
    sess.execute = AsyncMock()
    return sess


async def test_insert_resolved_writes_status_resolved_with_denorm_is_correct() -> None:
    """The happy-path INSERT must set ``status='resolved'`` AND the
    denormalised ``is_correct`` flag.

    Pins the contract the recompute query relies on: the SUM over
    ``is_correct=True`` rows is the source's correct count.
    """
    sess = _fake_session()
    repo = OutcomeRepository(sess)

    claim_id = uuid4()
    resolved_at = datetime.now(tz=UTC)

    record = await repo.insert_resolved(
        claim_id=claim_id,
        source_id="tg:" + "a" * 32,
        observed_return_pct=5.2,
        is_correct=True,
        price_t0_usd=100.0,
        price_resolve_usd=105.2,
        price_t0_ref="2026-06-02T10:00:00+00:00",
        price_resolve_ref="2026-06-02T12:00:00+00:00",
        resolved_at=resolved_at,
    )

    sess.add.assert_called_once()
    added = sess.add.call_args[0][0]
    assert isinstance(added, Outcome)
    assert added.claim_id == claim_id
    assert added.status == "resolved"
    assert added.observed_return_pct == pytest.approx(5.2)
    assert added.is_correct is True
    assert added.price_t0_usd == pytest.approx(100.0)
    assert added.price_resolve_usd == pytest.approx(105.2)
    sess.flush.assert_awaited_once()
    sess.refresh.assert_awaited_once_with(added)
    assert record.status == "resolved"


async def test_insert_rejected_asset_mutation_writes_nulls() -> None:
    """Q-023 rows must leave the return + is_correct columns NULL.

    The CHECK constraints
    (``ck_outcomes_resolved_has_return`` /
    ``ck_outcomes_resolved_has_is_correct``) require this — and the
    reputation aggregation relies on ``WHERE status='resolved'`` to
    skip them, so a rejection row that accidentally carried a
    return value would warp the user's hit rate.
    """
    sess = _fake_session()
    repo = OutcomeRepository(sess)

    claim_id = uuid4()
    record = await repo.insert_rejected_asset_mutation(
        claim_id=claim_id,
        source_id="tg:" + "b" * 32,
        resolved_at=datetime.now(tz=UTC),
    )

    added = sess.add.call_args[0][0]
    assert isinstance(added, Outcome)
    assert added.status == "rejected_asset_mutation"
    assert added.observed_return_pct is None
    assert added.is_correct is None
    assert record.status == "rejected_asset_mutation"


async def test_repository_does_not_commit() -> None:
    """Caller owns the TX boundary — the repo flushes only.

    Lets the resolver commit the outcome row + enqueue together (or
    roll both back on failure).
    """
    sess = _fake_session()
    sess.commit = AsyncMock()
    repo = OutcomeRepository(sess)
    await repo.insert_resolved(
        claim_id=uuid4(),
        source_id="tg:" + "c" * 32,
        observed_return_pct=1.0,
        is_correct=True,
        price_t0_usd=1.0,
        price_resolve_usd=1.01,
        price_t0_ref="ref-t0",
        price_resolve_ref="ref-t1",
        resolved_at=datetime.now(tz=UTC),
    )
    sess.commit.assert_not_awaited()


async def _scalar_returning(value: Any) -> MagicMock:
    """Build a Result-shaped mock that returns ``value`` via ``.scalars().first()``."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = value
    result.scalars.return_value = scalars
    return result


async def test_latest_for_claim_returns_none_when_no_outcome() -> None:
    """The idempotency guard reads this and proceeds only on ``None``."""
    sess = _fake_session()
    sess.execute.return_value = await _scalar_returning(None)
    repo = OutcomeRepository(sess)
    assert await repo.latest_for_claim(uuid4()) is None


async def test_latest_for_claim_projects_row_to_record() -> None:
    """A returned ORM row must project cleanly through ``_to_record``."""
    sess = _fake_session()
    row = MagicMock(spec=Outcome)
    row.id = uuid4()
    row.claim_id = uuid4()
    row.source_id = "tg:" + "a" * 32
    row.status = "resolved"
    row.observed_return_pct = 5.0
    row.is_correct = True
    row.price_t0_usd = 100.0
    row.price_resolve_usd = 105.0
    row.price_t0_ref = "t0"
    row.price_resolve_ref = "t1"
    row.supersedes_outcome_id = None
    row.resolved_at = datetime.now(tz=UTC)
    sess.execute.return_value = await _scalar_returning(row)

    repo = OutcomeRepository(sess)
    record = await repo.latest_for_claim(row.claim_id)

    assert record is not None
    assert record.status == "resolved"
    assert record.observed_return_pct == pytest.approx(5.0)
    assert record.is_correct is True


async def test_count_resolved_and_correct_for_source_compiles_query() -> None:
    """The aggregation query must compile and reach the session.

    We don't pin the literal SQL (dialect-specific), but we DO pin
    that the WHERE clause filters on ``status='resolved'`` so Q-023
    rejection rows don't inflate the denominator.
    """
    sess = _fake_session()
    result = MagicMock()
    result.one.return_value = type("Row", (), {"resolved": 4, "correct": 3})()
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    resolved, correct = await repo.count_resolved_and_correct_for_source(
        "tg:" + "z" * 32,
    )

    assert resolved == 4
    assert correct == 3
    sess.execute.assert_awaited_once()
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "outcomes" in compiled.lower()
    assert "status" in compiled.lower()
    assert "resolved" in compiled.lower()


async def test_fetch_resolved_for_reputation_projects_rows_to_records() -> None:
    """Per-bet fetch must JOIN claims for direction + conviction and
    project each row into a ``ResolvedOutcomeForReputation``."""
    sess = _fake_session()
    rows = [
        type(
            "Row",
            (),
            {
                "direction": "long",
                "conviction": "strong",
                "observed_return_pct": 5.0,
                "is_correct": True,
            },
        )(),
        type(
            "Row",
            (),
            {
                "direction": "short",
                "conviction": "medium",
                "observed_return_pct": -2.0,
                "is_correct": True,
            },
        )(),
    ]
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    records = await repo.fetch_resolved_for_reputation("tg:" + "a" * 32)

    assert len(records) == 2
    assert records[0].direction == "long"
    assert records[0].conviction == "strong"
    assert records[0].observed_return_pct == pytest.approx(5.0)
    assert records[0].is_correct is True
    assert records[1].direction == "short"
    assert records[1].observed_return_pct == pytest.approx(-2.0)


async def test_fetch_resolved_for_reputation_query_joins_claims() -> None:
    """The compiled SQL must JOIN ``claims`` so the conviction column
    is reachable. A future refactor that drops the join would silently
    break PnL math."""
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    records = await repo.fetch_resolved_for_reputation("tg:" + "z" * 32)

    assert records == []
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "outcomes" in compiled
    assert "claims" in compiled
    assert "conviction" in compiled
    assert "direction" in compiled
    assert "join" in compiled
    # Filter must still match ``status='resolved'`` exactly — same
    # rule as the count query so the two views agree.
    assert "'resolved'" in compiled


# ---------------------------------------------------------------------------
# ``fetch_resolved_for_tickers`` — per-ticker breakdown fetch.
# ---------------------------------------------------------------------------


async def test_fetch_resolved_for_tickers_projects_rows_with_asset_symbol() -> None:
    """Per-ticker fetch must carry ``asset_symbol`` in addition to the
    reputation-fetch fields. The application layer groups by ticker
    before running the scoring pipeline."""
    sess = _fake_session()
    rows = [
        type(
            "Row",
            (),
            {
                "direction": "long",
                "conviction": "strong",
                "asset_symbol": "TON",
                "observed_return_pct": 7.6,
                "is_correct": True,
            },
        )(),
        type(
            "Row",
            (),
            {
                "direction": "short",
                "conviction": "medium",
                "asset_symbol": "BTC",
                "observed_return_pct": -2.0,
                "is_correct": True,
            },
        )(),
    ]
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    records = await repo.fetch_resolved_for_tickers("tg:" + "a" * 32)

    assert len(records) == 2
    assert records[0].asset_symbol == "TON"
    assert records[0].direction == "long"
    assert records[0].conviction == "strong"
    assert records[0].observed_return_pct == pytest.approx(7.6)
    assert records[1].asset_symbol == "BTC"
    assert records[1].observed_return_pct == pytest.approx(-2.0)


async def test_fetch_resolved_for_tickers_query_joins_claims_for_asset_symbol() -> None:
    """The compiled SQL must JOIN ``claims`` for ``asset_symbol`` AND
    filter on ``status='resolved'`` — same shape as the reputation
    fetch, just with the extra projected column."""
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    await repo.fetch_resolved_for_tickers("tg:" + "z" * 32)

    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "outcomes" in compiled
    assert "claims" in compiled
    assert "asset_symbol" in compiled
    assert "join" in compiled
    assert "'resolved'" in compiled


# ---------------------------------------------------------------------------
# ``fetch_history_page`` — paginated newest-first list + total in one query.
# ---------------------------------------------------------------------------


def _history_row(
    *,
    claim_id: Any,
    asset_symbol: str,
    direction: str,
    horizon_seconds: int,
    observed_return_pct: float,
    is_correct: bool,
    resolved_at: datetime,
    total: int,
) -> Any:
    """Build a Row-shaped fake (window-function ``total`` included)."""
    return type(
        "Row",
        (),
        {
            "claim_id": claim_id,
            "asset_symbol": asset_symbol,
            "direction": direction,
            "horizon_seconds": horizon_seconds,
            "observed_return_pct": observed_return_pct,
            "is_correct": is_correct,
            "resolved_at": resolved_at,
            "total": total,
        },
    )()


async def test_fetch_history_page_projects_rows_and_carries_total_from_window() -> None:
    """Each row lands as an ``OutcomeHistoryEntry``; the window
    function ``total`` is read off the first row and returned
    alongside the page. Single SELECT — one round-trip."""
    claim_id = uuid4()
    resolved_at = datetime.now(tz=UTC)
    sess = _fake_session()
    rows = [
        _history_row(
            claim_id=claim_id,
            asset_symbol="TON",
            direction="short",
            horizon_seconds=86_400,
            observed_return_pct=-7.6,
            is_correct=True,
            resolved_at=resolved_at,
            total=42,
        ),
    ]
    result = MagicMock()
    result.all.return_value = rows
    sess.execute.return_value = result

    repo = OutcomeRepository(sess)
    records, total = await repo.fetch_history_page(
        "tg:" + "a" * 32,
        as_of=datetime.now(tz=UTC),
        limit=30,
        offset=0,
    )

    assert len(records) == 1
    assert records[0].claim_id == claim_id
    assert records[0].asset_symbol == "TON"
    assert records[0].direction == "short"
    assert records[0].horizon_seconds == 86_400
    assert records[0].observed_return_pct == pytest.approx(-7.6)
    assert records[0].is_correct is True
    assert records[0].resolved_at == resolved_at
    assert total == 42
    # One round-trip: a single ``execute`` for the populated-page path.
    assert sess.execute.await_count == 1


async def test_fetch_history_page_empty_page_runs_followup_count() -> None:
    """When the page is empty (offset past the last row), the window
    function returns no rows and we have no ``total`` to read. The
    repository then runs a COUNT-only query so ``has_more`` is honest.
    Pinned because a regression here would let the route silently say
    ``has_more=False`` when there's actually more data, or vice versa.
    """
    sess = _fake_session()

    page_result = MagicMock()
    page_result.all.return_value = []

    count_result = MagicMock()
    count_result.scalar.return_value = 12

    sess.execute = AsyncMock(side_effect=[page_result, count_result])

    repo = OutcomeRepository(sess)
    records, total = await repo.fetch_history_page(
        "tg:" + "z" * 32,
        as_of=datetime.now(tz=UTC),
        limit=30,
        offset=999,
    )

    assert records == []
    assert total == 12
    # Two round-trips on the empty-tail branch.
    assert sess.execute.await_count == 2


async def test_fetch_history_page_query_filters_as_of_and_supersedes_chain() -> None:
    """The compiled SQL must:

    1. Filter ``status='resolved'`` (existing contract).
    2. Filter ``resolved_at <= :as_of`` (frozen window — stops a new
       outcome from shifting paged rows during the user's session).
    3. Exclude superseded rows via ``NOT EXISTS`` — MVP §15 invariant
       6 says corrections INSERT a new outcome with
       ``supersedes_outcome_id`` pointing at the prior head; the
       user-facing list shows only the chain head.

    Also pins the ``ORDER BY resolved_at DESC``, the ``LIMIT`` +
    ``OFFSET``, and the window-function ``COUNT(*) OVER ()`` so a
    refactor that drops any of them breaks loudly.
    """
    sess = _fake_session()
    result = MagicMock()
    result.all.return_value = []
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    sess.execute = AsyncMock(side_effect=[result, count_result])

    repo = OutcomeRepository(sess)
    await repo.fetch_history_page(
        "tg:" + "z" * 32,
        as_of=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        limit=30,
        offset=60,
    )

    stmt = sess.execute.await_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "outcomes" in compiled
    assert "claims" in compiled
    assert "join" in compiled
    assert "'resolved'" in compiled
    # Frozen window.
    assert "resolved_at <=" in compiled
    # Supersedes-chain head filter (MVP §15 invariant 6).
    assert "not " in compiled
    assert "exists" in compiled
    assert "supersedes_outcome_id" in compiled
    # Newest-first is the contract.
    assert "order by" in compiled
    assert "resolved_at desc" in compiled
    # Window function for the total.
    assert "count(*) over" in compiled
    # Pagination knobs reached the SQL.
    assert "limit 30" in compiled
    assert "offset 60" in compiled


async def test_fetch_history_page_empty_count_query_also_filters_supersedes_and_as_of() -> None:
    """The follow-up COUNT-only query on the empty-tail branch must
    apply the SAME filter set as the page query — otherwise total
    and page would disagree, and ``has_more`` would lie."""
    sess = _fake_session()
    page_result = MagicMock()
    page_result.all.return_value = []
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    sess.execute = AsyncMock(side_effect=[page_result, count_result])

    repo = OutcomeRepository(sess)
    await repo.fetch_history_page(
        "tg:" + "z" * 32,
        as_of=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        limit=30,
        offset=999,
    )

    count_stmt = sess.execute.await_args_list[1].args[0]
    compiled = str(count_stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "count" in compiled
    assert "'resolved'" in compiled
    assert "resolved_at <=" in compiled
    assert "supersedes_outcome_id" in compiled


async def test_fetch_history_page_treats_null_scalar_as_zero_total() -> None:
    """The empty-tail COUNT must default to 0 on a NULL scalar so an
    edge case doesn't crash the route."""
    sess = _fake_session()
    page_result = MagicMock()
    page_result.all.return_value = []
    count_result = MagicMock()
    count_result.scalar.return_value = None
    sess.execute = AsyncMock(side_effect=[page_result, count_result])

    repo = OutcomeRepository(sess)
    _records, total = await repo.fetch_history_page(
        "tg:" + "z" * 32,
        as_of=datetime.now(tz=UTC),
        limit=30,
        offset=0,
    )
    assert total == 0
