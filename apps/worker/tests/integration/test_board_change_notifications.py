"""Live-DB integration tests for the board-change notification hook.

The pure classifier is exhaustively unit-tested in
``packages/python/merlin_core/tests/application/use_cases/test_board_diff.py``;
the templates in
``packages/python/merlin_infrastructure/tests/unit/test_telegram_templates.py``.
This module verifies the SQL plumbing and the end-to-end fan-out against
a real Postgres:

* ``SourceReputationRepository.current_board_rows`` returns the latest
  snapshot per source and excludes cold-start sources.
* ``_emit_board_change_notifications`` reads the true (post) board,
  reverts the resolving source to ``prev`` to reconstruct the pre board,
  classifies, resolves Telegram ids in one batch, and DMs the resolving
  user AND any displaced "bumped" user — while a cold-start user with no
  closed calls and the unchanged leader get nothing.

Marked ``integration`` + ``slow``: ``make test-int`` runs this;
``make ci-fast`` skips it. A reachable Docker daemon is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from merlin_core.application.ports import SourceReputationRecord
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _async_dsn(sync_dsn: str) -> str:
    return (
        sync_dsn.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        .replace("postgresql://", "postgresql+asyncpg://")
        .replace("postgresql+psycopg://", "postgresql+asyncpg://")
    )


def _apply_alembic_migrations(async_dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[4]
    alembic_dir = repo_root / "packages" / "python" / "merlin_infrastructure" / "alembic"
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", async_dsn)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def migrated_dsn(postgres_container: PostgresContainer) -> str:
    async_dsn = _async_dsn(postgres_container.get_connection_url())
    _apply_alembic_migrations(async_dsn)
    return async_dsn


@pytest.fixture
async def db_session(migrated_dsn: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_dsn)
    # Truncate the tables this module touches before each test. CASCADE
    # follows the source_reputation_history / source_identity FKs.
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sources CASCADE"))
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_source(
    session: AsyncSession,
    *,
    source_id: str,
    tg_user_id: int,
    display_name: str | None = None,
) -> None:
    from merlin_infrastructure.db.models import Source, SourceIdentity

    session.add(Source(source_id=source_id, display_name=display_name, kind="user"))
    session.add(
        SourceIdentity(source_id=source_id, tg_user_id=tg_user_id, tg_handle=None),
    )


async def _seed_snapshot(
    session: AsyncSession,
    *,
    source_id: str,
    sort_score: float,
    accuracy_pct: float | None = 66.67,
    weighted_pnl_pct: float | None = 1.0,
    outcomes_resolved: int = 3,
    outcomes_correct: int = 2,
    effective_at: datetime | None = None,
) -> None:
    from merlin_infrastructure.db.models import SourceReputationHistory

    session.add(
        SourceReputationHistory(
            source_id=source_id,
            outcomes_resolved=outcomes_resolved,
            outcomes_correct=outcomes_correct,
            accuracy_pct=accuracy_pct,
            weighted_pnl_pct=weighted_pnl_pct,
            sort_score=sort_score,
            triggering_outcome_id=None,
            effective_at=effective_at or datetime.now(tz=UTC),
        ),
    )


# ---------------------------------------------------------------------------
# current_board_rows
# ---------------------------------------------------------------------------


async def test_current_board_rows_latest_per_source_excludes_cold_start(
    db_session: AsyncSession,
) -> None:
    from merlin_infrastructure.db.repositories import SourceReputationRepository

    # Source A: two snapshots — the newer (higher effective_at) wins.
    await _seed_source(db_session, source_id="tg:aaaa", tg_user_id=111)
    await _seed_snapshot(
        db_session,
        source_id="tg:aaaa",
        sort_score=1.0,
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await _seed_snapshot(
        db_session,
        source_id="tg:aaaa",
        sort_score=9.0,
        effective_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # Cold-start source: accuracy_pct NULL ⇔ outcomes_resolved 0.
    await _seed_source(db_session, source_id="tg:cold", tg_user_id=222)
    await _seed_snapshot(
        db_session,
        source_id="tg:cold",
        sort_score=0.0,
        accuracy_pct=None,
        weighted_pnl_pct=None,
        outcomes_resolved=0,
        outcomes_correct=0,
    )
    await db_session.commit()

    rows = await SourceReputationRepository(db_session).current_board_rows()
    by_id = {r.source_id: r for r in rows}

    assert set(by_id) == {"tg:aaaa"}  # cold-start excluded
    assert by_id["tg:aaaa"].sort_score == 9.0  # latest snapshot only


# ---------------------------------------------------------------------------
# End-to-end hook fan-out
# ---------------------------------------------------------------------------


async def test_hook_notifies_resolving_and_bumped_only(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C climbs #3 -> #2, bumping B #2 -> #3. Both are DMed; the
    unchanged leader A, the lower ranks D/E, and a cold-start user Z
    (no closed calls) get nothing."""
    from merlin_infrastructure.notify.fake import FakeNotifier
    from merlin_worker.config import Settings
    from merlin_worker.jobs.recompute_source_reputation import (
        _emit_board_change_notifications,
    )

    # POST board (current DB state): A #1, C #2, B #3, D #4, E #5.
    await _seed_source(db_session, source_id="tg:A", tg_user_id=1001)
    await _seed_source(db_session, source_id="tg:C", tg_user_id=1003)
    await _seed_source(db_session, source_id="tg:B", tg_user_id=1002)
    await _seed_source(db_session, source_id="tg:D", tg_user_id=1004)
    await _seed_source(db_session, source_id="tg:E", tg_user_id=1005)
    await _seed_snapshot(db_session, source_id="tg:A", sort_score=50.0)
    await _seed_snapshot(db_session, source_id="tg:C", sort_score=45.0)
    await _seed_snapshot(db_session, source_id="tg:B", sort_score=40.0)
    await _seed_snapshot(db_session, source_id="tg:D", sort_score=20.0)
    await _seed_snapshot(db_session, source_id="tg:E", sort_score=10.0)
    # Cold-start Z — has an identity but no eligible snapshot.
    await _seed_source(db_session, source_id="tg:Z", tg_user_id=1009)
    await _seed_snapshot(
        db_session,
        source_id="tg:Z",
        sort_score=0.0,
        accuracy_pct=None,
        weighted_pnl_pct=None,
        outcomes_resolved=0,
        outcomes_correct=0,
    )
    await db_session.commit()

    # ``prev`` for C: a lower score (25.0) that, when reverted, puts C at
    # #3 in the pre board — so the diff is C #3 -> #2 (climbed) and the
    # displaced B #2 -> #3 (dropped).
    prev_c = SourceReputationRecord(
        id=uuid4(),
        source_id="tg:C",
        outcomes_resolved=2,
        outcomes_correct=1,
        accuracy_pct=50.0,
        weighted_pnl_pct=0.5,
        sort_score=25.0,
        triggering_outcome_id=None,
        effective_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    recorder = FakeNotifier()
    monkeypatch.setattr(
        "merlin_infrastructure.notify.telegram.notifier.TelegramNotifier",
        lambda *, bot_token: recorder,
    )

    await _emit_board_change_notifications(
        session=db_session,
        settings=Settings(environment="test", sentry_dsn=""),
        source_id="tg:C",
        prev=prev_c,
    )

    by_tg = {call.tg_user_id: call.text for call in recorder.calls}
    # Exactly the resolving user (C=1003) and the bumped user (B=1002).
    assert set(by_tg) == {1003, 1002}
    assert "climbed from #3 to #2" in by_tg[1003]
    assert "you fell from #2 to #3" in by_tg[1002]
    # Unchanged leader, lower ranks, and the cold-start user are silent.
    assert 1001 not in by_tg  # A (still #1)
    assert 1004 not in by_tg  # D
    assert 1005 not in by_tg  # E
    assert 1009 not in by_tg  # Z (no closed calls — never on the board)


async def test_hook_silent_when_only_deep_field_moves(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolving source that stays deep in the field (e.g. #12 with no
    top-10 / top-3 crossing) produces no DMs — the candidate filter cuts
    it before any identity lookup."""
    from merlin_infrastructure.notify.fake import FakeNotifier
    from merlin_worker.config import Settings
    from merlin_worker.jobs.recompute_source_reputation import (
        _emit_board_change_notifications,
    )

    # 15-source board; the resolving source "tg:s12" sits at #12 both
    # before and after (its score nudged but rank unchanged).
    for i in range(1, 16):
        await _seed_source(db_session, source_id=f"tg:s{i:02d}", tg_user_id=2000 + i)
        await _seed_snapshot(db_session, source_id=f"tg:s{i:02d}", sort_score=float(100 - i))
    await db_session.commit()

    prev_s12 = SourceReputationRecord(
        id=uuid4(),
        source_id="tg:s12",
        outcomes_resolved=3,
        outcomes_correct=1,
        accuracy_pct=33.3,
        weighted_pnl_pct=-1.0,
        sort_score=88.4,  # still between s11 (89) and s13 (87) -> stays #12
        triggering_outcome_id=None,
        effective_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    recorder = FakeNotifier()
    monkeypatch.setattr(
        "merlin_infrastructure.notify.telegram.notifier.TelegramNotifier",
        lambda *, bot_token: recorder,
    )

    await _emit_board_change_notifications(
        session=db_session,
        settings=Settings(environment="test", sentry_dsn=""),
        source_id="tg:s12",
        prev=prev_s12,
    )

    assert recorder.calls == []
