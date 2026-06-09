"""Unit tests for ``ClaimRepository``.

These exercise the query shape without a live Postgres — the goal is
to pin the contract the route + bot depend on (``list_open_by_source``
orders by ``resolves_at`` and filters on ``resolves_at > now()``;
``count_resolved_by_source`` returns an int). The leaderboard read
lives on ``SourceReputationRepository`` — it queries
``source_reputation_history``, not raw claims. End-to-end testing
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
        horizon_seconds=1_209_600,
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
    assert added.horizon_seconds == 1_209_600
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


async def test_create_persists_price_usd_at_submit() -> None:
    """``create`` must forward ``price_usd_at_submit`` to the ORM row
    and project it back through the returned ``ClaimRecord``.

    The /alpha receipt + /me both read this value off the row; if
    the repo silently dropped it, every freshly-submitted claim
    would render without an entry-price line even when the API
    captured the snapshot. This pins the write-path contract.
    """
    sess = _fake_session()
    repo = ClaimRepository(sess)

    submitted_at = datetime.now(tz=UTC)
    record = await repo.create(
        claim_id=uuid4(),
        source_id="tg:" + "a" * 32,
        asset_coingecko_id="ethereum",
        asset_symbol="ETH",
        asset_name="Ethereum",
        direction="long",
        horizon_seconds=1_209_600,
        conviction="strong",
        thesis_text="Vitalik staking news",
        asset_query="ETH",
        submitted_at=submitted_at,
        resolves_at=submitted_at + timedelta(days=14),
        parsed_via="freeform_llm",
        extracted_fields={},
        parse_metadata={"parsed_via": "freeform_llm"},
        price_usd_at_submit=2438.12,
    )
    added = sess.add.call_args[0][0]
    assert added.price_usd_at_submit == pytest.approx(2438.12), (
        "ClaimRepository.create did not put price_usd_at_submit on "
        "the ORM Claim row — the column writes NULL even when the "
        "API captured a snapshot."
    )
    # And the returned ClaimRecord round-trips the value back to the
    # route (which echoes it on ClaimResponse for the bot's receipt).
    assert record.price_usd_at_submit == pytest.approx(2438.12)


async def test_create_defaults_price_usd_at_submit_to_none() -> None:
    """Existing call sites that don't pass ``price_usd_at_submit``
    (the form-flow path, legacy tests) must still work — the kwarg
    defaults to ``None``, persists NULL, and the projected record
    carries ``None`` cleanly through to the wire payload.
    """
    sess = _fake_session()
    repo = ClaimRepository(sess)

    submitted_at = datetime.now(tz=UTC)
    record = await repo.create(
        claim_id=uuid4(),
        source_id="tg:" + "c" * 32,
        asset_coingecko_id="bitcoin",
        asset_symbol="BTC",
        asset_name="Bitcoin",
        direction="short",
        horizon_seconds=604_800,
        conviction="medium",
        thesis_text="...",
        asset_query=None,
        submitted_at=submitted_at,
        resolves_at=submitted_at + timedelta(days=7),
        parsed_via="form",
        extracted_fields={},
        parse_metadata={"parsed_via": "form"},
        # price_usd_at_submit deliberately omitted
    )
    added = sess.add.call_args[0][0]
    assert added.price_usd_at_submit is None
    assert record.price_usd_at_submit is None


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
        horizon_seconds=2_592_000,
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


def _fake_claim_row(*, asset_symbol: str, resolves_at: datetime, **overrides: Any) -> MagicMock:
    """Build a MagicMock shaped like a SQLAlchemy-returned ``Claim``.

    Every column the repository's ``_to_record`` projection touches
    is set explicitly so MagicMock can't silently auto-attribute a
    non-``None`` Mock for a missing assignment — exactly the failure
    mode a forgotten line in ``_to_record`` would cause.
    """
    now = datetime.now(tz=UTC)
    row = MagicMock(spec=Claim)
    row.id = uuid4()
    row.source_id = "tg:abc"
    row.asset_coingecko_id = "x"
    row.asset_symbol = asset_symbol
    row.asset_name = asset_symbol.capitalize()
    row.direction = "long"
    row.horizon_seconds = 1_209_600
    row.conviction = "strong"
    row.thesis_text = "..."
    row.asset_query = asset_symbol
    row.submitted_at = now
    row.resolves_at = resolves_at
    row.parsed_via = "freeform_llm"
    row.price_usd_at_submit = None
    row.extracted_fields = {}
    row.parse_metadata = {}
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


async def test_list_open_by_source_filters_and_orders() -> None:
    """The query must filter on ``resolves_at > now()`` and order ASC.

    We don't execute against a real DB here; instead we verify the
    statement compiles (catches typos in column references) and the
    returned rows project cleanly through ``_to_record``.

    We also pin the read-side ``price_usd_at_submit`` projection by
    seeding one row with a captured snapshot (ETH) and one with NULL
    (BTC — simulates CoinGecko down at submit / legacy row). A bug
    in ``_to_record`` (e.g. forgetting the new field) would make ETH
    silently lose the column on the way to ``/me``.
    """
    sess = _fake_session()
    now = datetime.now(tz=UTC)
    row_eth = _fake_claim_row(
        asset_symbol="ETH",
        resolves_at=now + timedelta(days=14),
        price_usd_at_submit=2438.12,
    )
    row_btc = _fake_claim_row(
        asset_symbol="BTC",
        resolves_at=now + timedelta(days=2),
        price_usd_at_submit=None,
    )
    sess.execute.return_value = await _scalars_returning([row_btc, row_eth])

    repo = ClaimRepository(sess)
    records = await repo.list_open_by_source("tg:abc")

    sess.execute.assert_awaited_once()
    assert len(records) == 2
    assert [r.asset_symbol for r in records] == ["BTC", "ETH"]

    btc_record, eth_record = records
    assert btc_record.price_usd_at_submit is None
    assert eth_record.price_usd_at_submit == pytest.approx(2438.12)

    # Compile the statement and inspect for the must-have predicates.
    # If the column references typo, the compile step itself raises.
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "claims" in compiled.lower()
    assert "source_id" in compiled
    assert "resolves_at" in compiled
    assert "order by" in compiled.lower()

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


async def test_count_open_global_returns_calls_and_distinct_callers() -> None:
    """``count_open_global`` projects ``(COUNT(*), COUNT(DISTINCT source_id))``
    to two ints in a single round-trip.

    Pins the wire contract the ``/board`` route reads: the first
    int is open calls, the second is distinct sources. Order
    matters — flipping them would put ``"4 open calls by 22 users"``
    on the bot footer, which is nonsense (callers ≤ calls always).
    """
    sess = _fake_session()
    row = MagicMock()
    row.one.return_value = (22, 4)
    sess.execute.return_value = row

    repo = ClaimRepository(sess)
    open_calls, open_callers = await repo.count_open_global()

    assert open_calls == 22
    assert open_callers == 4

    # Compile the statement and inspect for the must-have shape.
    # Two COUNTs in the projection, no ``source_id`` predicate in
    # the WHERE (this is the platform-wide path), and the
    # ``resolves_at > :now`` future-only filter.
    stmt = sess.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "claims" in compiled.lower()
    assert "resolves_at" in compiled
    # Two COUNT() expressions in the SELECT list (the second one is
    # the distinct one); the literal "distinct" appears in compiled SQL.
    assert compiled.lower().count("count(") >= 2
    assert "distinct" in compiled.lower()
    # No per-source filter: the platform-wide path must not narrow on
    # source_id. A regression that adds one would silently turn the
    # bot footer into "X open calls by 1 user" for every caller.
    assert "source_id =" not in compiled.replace(" ", "")


async def test_count_open_global_returns_zero_on_cold_start() -> None:
    """Empty ``claims`` table → ``(0, 0)``.

    ``COUNT`` never returns NULL on a non-grouped query, but the
    repo's defensive ``int(... or 0)`` makes a future query rewrite
    that lets a NULL through still produce a safe zero rather than
    crashing on ``int(None)``.
    """
    sess = _fake_session()
    row = MagicMock()
    row.one.return_value = (0, 0)
    sess.execute.return_value = row

    repo = ClaimRepository(sess)
    assert await repo.count_open_global() == (0, 0)


async def test_count_open_global_defends_against_null_columns() -> None:
    """Defensive: if a future query rewrite lets a NULL leak into
    either column, the helper coerces to 0 rather than raising
    ``TypeError: int() argument must be a string... not NoneType``.

    Realistic trigger would be a subquery / window function rewrite
    where the outer projection NULLs the per-group count. The
    failure mode is silent on every cold-start day and explodes on
    the day the rewrite ships — pinning the fallback keeps the
    leaderboard rendering even then.
    """
    sess = _fake_session()
    row = MagicMock()
    row.one.return_value = (None, None)
    sess.execute.return_value = row

    repo = ClaimRepository(sess)
    assert await repo.count_open_global() == (0, 0)


async def test_claim_repository_has_no_leaderboard_method() -> None:
    """The leaderboard read moved to ``SourceReputationRepository``.

    Pins the layering decision: anything that wants to rank sources
    by accuracy must go through the reputation-history repository.
    A regression that re-adds ``leaderboard`` here is a sign that
    someone reverted the per-snapshot read path back to a per-claim
    aggregation — which is the historical empty-list bug.
    """
    repo = ClaimRepository(_fake_session())
    assert not hasattr(repo, "leaderboard"), (
        "ClaimRepository regained a .leaderboard method. Move it to "
        "SourceReputationRepository — that's where source_reputation_history "
        "lives."
    )
