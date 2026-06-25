"""Unit tests for ``EngagementRepository`` (Q-031 first-call nudge).

Same strategy as ``test_claim_repository``: a session-shaped mock that
records ``add`` / ``flush`` / ``execute`` and returns canned results.
We pin the eligibility-query shape (the rules live in the SQL) and the
``record_nudge`` insert contract without a live Postgres.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_infrastructure.db.models import EngagementNudge
from merlin_infrastructure.db.repositories.engagement_repository import (
    FIRST_CALL_NUDGE_KIND,
    EngagementRepository,
)

pytestmark = pytest.mark.asyncio


def _fake_session() -> MagicMock:
    sess = MagicMock()
    sess.add = MagicMock()
    sess.flush = AsyncMock()
    sess.execute = AsyncMock()
    return sess


async def _all_returning(rows: list[Any]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


async def test_list_first_call_nudge_targets_projects_source_ids() -> None:
    """Returns the bare ``source_id`` strings (the cron maps to TG ids later)."""
    sess = _fake_session()
    sess.execute.return_value = await _all_returning([("tg:aaa",), ("tg:bbb",)])

    repo = EngagementRepository(sess)
    ids = await repo.list_first_call_nudge_targets(
        cooldown_seconds=604_800,
        max_sends=2,
        min_account_age_seconds=86_400,
        limit=500,
    )

    sess.execute.assert_awaited_once()
    assert ids == ["tg:aaa", "tg:bbb"]


async def test_list_first_call_nudge_targets_query_shape() -> None:
    """Pin the eligibility SQL: named users, zero claims, cap + cooldown.

    Each rule maps to a clause; a regression that drops one would either
    spam (missing cooldown / cap) or nudge the wrong people (missing the
    name / claims / kind filters).
    """
    sess = _fake_session()
    sess.execute.return_value = await _all_returning([])

    repo = EngagementRepository(sess)
    assert (
        await repo.list_first_call_nudge_targets(
            cooldown_seconds=604_800,
            max_sends=2,
            min_account_age_seconds=86_400,
            limit=500,
        )
        == []
    )

    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()
    # Targets sources, anti-joins claims, gates on engagement_nudges.
    assert "sources" in compiled
    assert "claims" in compiled
    assert "engagement_nudges" in compiled
    # Rule 1: signed up + named + real user.
    assert "display_name is not null" in compiled
    assert "kind" in compiled
    # Rule 1: zero bets — NOT EXISTS over claims.
    assert "not" in compiled
    assert "exists" in compiled
    # Rule 3: lifetime cap via a count subquery compared to a bound.
    assert "count(" in compiled
    # Rule 2: cooldown recency on sent_at.
    assert "sent_at >" in compiled
    # Bounded + ordered (oldest accounts first).
    assert "order by" in compiled
    assert "limit" in compiled


async def test_record_nudge_inserts_a_row() -> None:
    """``record_nudge`` adds one ``engagement_nudges`` row and flushes."""
    sess = _fake_session()
    repo = EngagementRepository(sess)

    await repo.record_nudge(source_id="tg:" + "a" * 32)

    sess.add.assert_called_once()
    added = sess.add.call_args[0][0]
    assert isinstance(added, EngagementNudge)
    assert added.source_id == "tg:" + "a" * 32
    assert added.nudge_kind == FIRST_CALL_NUDGE_KIND
    sess.flush.assert_awaited_once()


async def test_record_nudge_does_not_commit() -> None:
    """Caller owns the unit of work — the repo only flushes."""
    sess = _fake_session()
    sess.commit = AsyncMock()
    repo = EngagementRepository(sess)

    await repo.record_nudge(source_id="tg:abc", nudge_kind="first_call")

    sess.commit.assert_not_awaited()
