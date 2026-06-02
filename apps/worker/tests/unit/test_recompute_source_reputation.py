"""Unit tests for ``recompute_source_reputation`` — reactive reputation update.

Pins:

* Reads ``(resolved, correct)`` from ``OutcomeRepository``.
* Derives ``accuracy_pct`` via the shared
  ``merlin_core.domain.scoring.accuracy_pct`` helper (so it matches
  the bot/leaderboard view by construction).
* INSERTs one ``source_reputation_history`` row per invocation.
* Cold-start path: zero outcomes → ``accuracy_pct=None``, but the
  row is still INSERTed (auditability for "we did recompute and
  found nothing").
* The TX commits at the end of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class _FakeOutcomeRepo:
    counts_by_source: dict[str, tuple[int, int]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def count_resolved_and_correct_for_source(
        self,
        source_id: str,
    ) -> tuple[int, int]:
        self.calls.append(source_id)
        return self.counts_by_source.get(source_id, (0, 0))


@dataclass
class _FakeReputationRepo:
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    async def insert_snapshot(self, **kwargs: Any) -> Any:
        self.snapshots.append(kwargs)
        return type("FakeSnapshot", (), kwargs)()


@dataclass
class _FakeSession:
    commits: int = 0

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


@dataclass
class _FakeSessionFactory:
    sessions: list[_FakeSession] = field(default_factory=list)

    def __call__(self) -> _FakeSession:
        s = _FakeSession()
        self.sessions.append(s)
        return s


@dataclass
class _RecomputeFixture:
    outcome_repo: _FakeOutcomeRepo
    reputation_repo: _FakeReputationRepo
    session_factory: _FakeSessionFactory


@pytest.fixture
def recompute_fixture(monkeypatch: pytest.MonkeyPatch) -> _RecomputeFixture:
    outcome_repo = _FakeOutcomeRepo()
    reputation_repo = _FakeReputationRepo()
    session_factory = _FakeSessionFactory()

    from merlin_infrastructure.db import repositories as _repos_mod
    from merlin_infrastructure.db import session as _session_mod

    monkeypatch.setattr(_repos_mod, "OutcomeRepository", lambda _s: outcome_repo)
    monkeypatch.setattr(
        _repos_mod,
        "SourceReputationRepository",
        lambda _s: reputation_repo,
    )
    monkeypatch.setattr(_session_mod, "get_session_factory", lambda _dsn: session_factory)

    return _RecomputeFixture(
        outcome_repo=outcome_repo,
        reputation_repo=reputation_repo,
        session_factory=session_factory,
    )


async def test_inserts_snapshot_with_accuracy_pct(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """3-correct-of-4 → ``accuracy_pct == 75.0``, snapshot row INSERTed."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "a" * 32
    recompute_fixture.outcome_repo.counts_by_source[src_id] = (4, 3)

    await _recompute_async(src_id)

    assert len(recompute_fixture.reputation_repo.snapshots) == 1
    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["source_id"] == src_id
    assert row["outcomes_resolved"] == 4
    assert row["outcomes_correct"] == 3
    assert row["accuracy_pct"] == pytest.approx(75.0)
    # TX committed.
    assert recompute_fixture.session_factory.sessions[0].commits == 1


async def test_cold_start_inserts_null_accuracy(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """Zero resolved outcomes → ``accuracy_pct=None`` but the row IS INSERTed.

    Preserves auditability: every recompute leaves a trail, even
    those that found nothing (e.g. recompute fired right after a
    Q-023 rejection that didn't touch the tally).
    """
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "b" * 32
    # No entry in counts_by_source → defaults to (0, 0).

    await _recompute_async(src_id)

    assert len(recompute_fixture.reputation_repo.snapshots) == 1
    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 0
    assert row["outcomes_correct"] == 0
    assert row["accuracy_pct"] is None


async def test_all_correct_yields_100pct(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """5-of-5 correct → 100%. Pins the divisor and rounding behaviour."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "c" * 32
    recompute_fixture.outcome_repo.counts_by_source[src_id] = (5, 5)

    await _recompute_async(src_id)

    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 5
    assert row["outcomes_correct"] == 5
    assert row["accuracy_pct"] == pytest.approx(100.0)


async def test_zero_correct_with_some_resolved_yields_zero_pct(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """0-of-3 correct → 0.0% (NOT ``None``). The cold-start signal is
    "no resolutions at all", not "you've been wrong every time"."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "d" * 32
    recompute_fixture.outcome_repo.counts_by_source[src_id] = (3, 0)

    await _recompute_async(src_id)

    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 3
    assert row["outcomes_correct"] == 0
    assert row["accuracy_pct"] == pytest.approx(0.0)
