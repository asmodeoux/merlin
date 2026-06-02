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
