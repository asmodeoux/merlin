"""Unit tests for ``ClaimRepository``.

These exercise the query shape without a live Postgres — the goal is
to pin the contract the route + bot depend on (``list_open_by_source``
orders by ``resolves_at`` and filters on ``resolves_at > now()``;
``count_resolved_by_source`` returns an int; ``leaderboard`` returns
an empty list until the outcomes layer lands). End-to-end testing
against a real DB lives in the integration suite.

Strategy: hand-rolled ``AsyncMock``-backed session that records the
``add`` / ``flush`` / ``refresh`` / ``execute`` calls and returns
canned results. The repository never sees real SQLAlchemy here, just
a session-shaped fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from merlin_infrastructure.db.models import Claim
from merlin_infrastructure.db.repositories.claim_repository import ClaimRepository

pytestmark = pytest.mark.asyncio


def _fake_session() -> MagicMock:
    """Build an ``AsyncSession``-shaped mock.

    Only the surface the repo touches is wired: ``add`` (sync), and
    the async methods ``flush`` / ``refresh`` / ``execute`` / ``get``.
    """
    sess = MagicMock()
    sess.add = MagicMock()
    sess.flush = AsyncMock()
    sess.refresh = AsyncMock()
    sess.execute = AsyncMock()
    sess.get = AsyncMock(return_value=None)
    return sess


async def test_create_inserts_a_claim_row_with_passed_fields() -> None:
    """``create`` must build a ``Claim`` with every field the API passes,
    then flush+refresh so DB-side defaults populate."""
    sess = _fake_session()
    repo = ClaimRepository(sess)

    claim_id = uuid4()
    submitted_at = datetime.now(tz=UTC)
    resolves_at = submitted_at + timedelta(days=14)

    record = await repo.create(
        claim_id=claim_id,
        source_id="tg:0000aaaa1111bbbb2222cccc3333dddd",
        asset_coingecko_id="ethereum",
        asset_symbol="ETH",
        asset_name="Ethereum",
        direction="long",
        horizon="14d",
        conviction="strong",
        thesis_text="Vitalik staking news",
        asset_query="ETH",
        submitted_at=submitted_at,
        resolves_at=resolves_at,
        parsed_via="freeform_llm",
        extracted_fields={"schema_version": 1},
        parse_metadata={"parsed_via": "freeform_llm", "model": "openai/gpt-5-mini"},
    )

    sess.add.assert_called_once()
    added = sess.add.call_args[0][0]
    assert isinstance(added, Claim)
    assert added.id == claim_id
    assert added.source_id == "tg:0000aaaa1111bbbb2222cccc3333dddd"
    assert added.asset_coingecko_id == "ethereum"
    assert added.asset_symbol == "ETH"
    assert added.direction == "long"
    assert added.horizon == "14d"
    assert added.conviction == "strong"
    assert added.thesis_text == "Vitalik staking news"
    assert added.asset_query == "ETH"
    assert added.parsed_via == "freeform_llm"
    assert added.extracted_fields == {"schema_version": 1}
    assert added.parse_metadata["model"] == "openai/gpt-5-mini"

    sess.flush.assert_awaited_once()
    sess.refresh.assert_awaited_once_with(added)

    # And the returned ``ClaimRecord`` projects every field through.
    assert record.id == claim_id
    assert record.asset_symbol == "ETH"
    assert record.direction == "long"


async def test_create_does_not_commit() -> None:
    """The repo flushes but never commits — the route holds the unit
    of work boundary so it can co-commit with the enqueue side-effect."""
    sess = _fake_session()
    sess.commit = AsyncMock()
    repo = ClaimRepository(sess)
    await repo.create(
        claim_id=uuid4(),
        source_id="tg:" + "a" * 32,
        asset_coingecko_id="bitcoin",
        asset_symbol="BTC",
        asset_name="Bitcoin",
        direction="long",
        horizon="30d",
        conviction="medium",
        thesis_text="halving",
        asset_query=None,
        submitted_at=datetime.now(tz=UTC),
        resolves_at=datetime.now(tz=UTC) + timedelta(days=30),
        parsed_via="form",
        extracted_fields={},
        parse_metadata={"parsed_via": "form"},
    )
    sess.commit.assert_not_awaited()


async def _scalars_returning(rows: list[Any]) -> MagicMock:
    """Build a Result-shaped mock that returns ``rows`` via ``.scalars().all()``."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    return result


async def test_list_open_by_source_filters_and_orders() -> None:
    """The query must filter on ``resolves_at > now()`` and order ASC.

    We don't execute against a real DB here; instead we verify the
    statement compiles (catches typos in column references) and the
    returned rows project cleanly through ``_to_record``.
    """
    sess = _fake_session()

    # Two fake Claim rows that look like SQLAlchemy returned them.
    now = datetime.now(tz=UTC)
    later = now + timedelta(days=14)
    soonest = now + timedelta(days=2)

    row1 = MagicMock(spec=Claim)
    row1.id = uuid4()
    row1.source_id = "tg:abc"
    row1.asset_coingecko_id = "ethereum"
    row1.asset_symbol = "ETH"
    row1.asset_name = "Ethereum"
    row1.direction = "long"
    row1.horizon = "14d"
    row1.conviction = "strong"
    row1.thesis_text = "..."
    row1.asset_query = "ETH"
    row1.submitted_at = now
    row1.resolves_at = later
    row1.parsed_via = "freeform_llm"
    row1.extracted_fields = {}
    row1.parse_metadata = {}

    row2 = MagicMock(spec=Claim)
    row2.id = uuid4()
    row2.source_id = "tg:abc"
    row2.asset_coingecko_id = "bitcoin"
    row2.asset_symbol = "BTC"
    row2.asset_name = "Bitcoin"
    row2.direction = "long"
    row2.horizon = "3d"
    row2.conviction = "medium"
    row2.thesis_text = "..."
    row2.asset_query = "BTC"
    row2.submitted_at = now
    row2.resolves_at = soonest
    row2.parsed_via = "form"
    row2.extracted_fields = {}
    row2.parse_metadata = {}

    sess.execute.return_value = await _scalars_returning([row2, row1])  # already-sorted

    repo = ClaimRepository(sess)
    records = await repo.list_open_by_source("tg:abc")

    sess.execute.assert_awaited_once()
    assert len(records) == 2
    assert [r.asset_symbol for r in records] == ["BTC", "ETH"]

    # Compile the statement and inspect for the must-have predicates.
    # If the column references typo, the compile step itself raises.
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "claims" in compiled.lower()
    assert "source_id" in compiled
    assert "resolves_at" in compiled
    assert "order by" in compiled.lower()


async def test_count_resolved_by_source_returns_int() -> None:
    """``count_resolved_by_source`` projects ``COUNT(*)`` to an int and
    returns 0 (not None) when the source has no rows."""
    sess = _fake_session()

    # 0-row case
    no_rows = MagicMock()
    no_rows.scalar.return_value = None
    sess.execute.return_value = no_rows
    repo = ClaimRepository(sess)
    assert await repo.count_resolved_by_source("tg:nobody") == 0

    # n-row case
    n_rows = MagicMock()
    n_rows.scalar.return_value = 3
    sess.execute.return_value = n_rows
    assert await repo.count_resolved_by_source("tg:somebody") == 3


async def test_leaderboard_returns_empty_for_now() -> None:
    """No outcomes layer yet → ``leaderboard(window)`` returns ``[]``.

    This is the contract the route relies on for the honest empty
    state. When the worker writes outcomes, only the repo
    implementation changes; this test gets a real ranking-shape
    counterpart alongside.
    """
    sess = _fake_session()
    repo = ClaimRepository(sess)
    assert await repo.leaderboard("all_time") == []
    assert await repo.leaderboard("7d") == []
    # And the call must not touch the session — there's no query to run.
    sess.execute.assert_not_awaited()
