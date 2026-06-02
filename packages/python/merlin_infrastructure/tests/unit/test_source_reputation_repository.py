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
    """The history row must include the denormalised ``accuracy_pct``."""
    sess = _fake_session()
    repo = SourceReputationRepository(sess)
    effective_at = datetime.now(tz=UTC)

    record = await repo.insert_snapshot(
        source_id="tg:" + "a" * 32,
        outcomes_resolved=4,
        outcomes_correct=3,
        accuracy_pct=75.0,
        triggering_outcome_id=None,
        effective_at=effective_at,
    )

    sess.add.assert_called_once()
    added = sess.add.call_args[0][0]
    assert isinstance(added, SourceReputationHistory)
    assert added.outcomes_resolved == 4
    assert added.outcomes_correct == 3
    assert added.accuracy_pct == pytest.approx(75.0)
    assert record.accuracy_pct == pytest.approx(75.0)


async def test_insert_snapshot_writes_null_accuracy_on_cold_start() -> None:
    """``outcomes_resolved=0`` MUST persist ``accuracy_pct=None``.

    The CHECK constraint ``ck_srh_accuracy_null_iff_cold_start``
    would reject anything else at the DB layer, but the application
    layer should also leave NULL by construction so a future
    migration that loosens the constraint doesn't quietly start
    persisting ``0.0`` (which the readers would misinterpret as a
    real hit rate).
    """
    sess = _fake_session()
    repo = SourceReputationRepository(sess)

    record = await repo.insert_snapshot(
        source_id="tg:" + "b" * 32,
        outcomes_resolved=0,
        outcomes_correct=0,
        accuracy_pct=None,
        triggering_outcome_id=None,
        effective_at=datetime.now(tz=UTC),
    )

    added = sess.add.call_args[0][0]
    assert added.outcomes_resolved == 0
    assert added.accuracy_pct is None
    assert record.accuracy_pct is None


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
    row.triggering_outcome_id = None
    row.effective_at = datetime.now(tz=UTC)
    sess.execute.return_value = await _scalar_returning(row)

    repo = SourceReputationRepository(sess)
    record = await repo.latest_for_source(row.source_id)

    assert record is not None
    assert record.outcomes_resolved == 10
    assert record.outcomes_correct == 7
    assert record.accuracy_pct == pytest.approx(70.0)


async def test_leaderboard_query_compiles() -> None:
    """The leaderboard statement must compile (no typos in column refs).

    We pin compile-time correctness; the actual ranking output is
    exercised in the integration test against a real DB. A regression
    here is the kind of "I renamed a column and broke the join" bug
    that doesn't show up until the first /board request.
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
    assert "source_reputation_history" in compiled.lower()
    assert "accuracy_pct" in compiled.lower()
    assert "order by" in compiled.lower()
