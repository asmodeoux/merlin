"""Unit tests for ``recompute_source_reputation`` — reactive reputation update.

Pins:

* Reads resolved outcome rows (with direction + conviction) from
  ``OutcomeRepository.fetch_resolved_for_reputation``.
* Derives ``accuracy_pct``, ``weighted_pnl_pct``, and ``sort_score``
  via the shared ``merlin_core.domain.scoring`` helpers so the bot,
  the API, and this worker agree by construction.
* INSERTs one ``source_reputation_history`` row per invocation.
* Cold-start path: zero outcomes → ``accuracy_pct=None``,
  ``weighted_pnl_pct=None``, ``sort_score=0.0``, but the row is still
  INSERTed (auditability for "we did recompute and found nothing").
* The TX commits at the end of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from merlin_core.application.ports import ResolvedOutcomeForReputation


@dataclass
class _FakeOutcomeRepo:
    rows_by_source: dict[str, list[ResolvedOutcomeForReputation]] = field(
        default_factory=dict,
    )
    calls: list[str] = field(default_factory=list)

    async def fetch_resolved_for_reputation(
        self,
        source_id: str,
    ) -> list[ResolvedOutcomeForReputation]:
        self.calls.append(source_id)
        return self.rows_by_source.get(source_id, [])


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


def _row(
    *,
    direction: str = "long",
    conviction: str = "medium",
    pct: float = 0.0,
    is_correct: bool = False,
) -> ResolvedOutcomeForReputation:
    """Fixture helper — readable per-row construction."""
    return ResolvedOutcomeForReputation(
        direction=direction,
        conviction=conviction,
        observed_return_pct=pct,
        is_correct=is_correct,
    )


async def test_inserts_snapshot_with_accuracy_and_pnl(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """3-correct-of-4 → accuracy 75.0%; PnL derived from per-bet rows.

    Pins the full snapshot shape — accuracy_pct, weighted_pnl_pct,
    sort_score all on the same row, all from the shared helpers.
    """
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "a" * 32
    recompute_fixture.outcome_repo.rows_by_source[src_id] = [
        _row(direction="long", conviction="strong", pct=5.0, is_correct=True),
        _row(direction="long", conviction="medium", pct=2.0, is_correct=True),
        _row(direction="short", conviction="medium", pct=-1.0, is_correct=True),
        _row(direction="long", conviction="weak", pct=-3.0, is_correct=False),
    ]

    await _recompute_async(src_id)

    assert len(recompute_fixture.reputation_repo.snapshots) == 1
    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["source_id"] == src_id
    assert row["outcomes_resolved"] == 4
    assert row["outcomes_correct"] == 3
    assert row["accuracy_pct"] == pytest.approx(75.0)
    # Contributions (signed for "user was right") + weights:
    #   long +5 strong    →  5 x 5  = +25 (w=5)
    #   long +2 medium    →  2 x 3  =  +6 (w=3)
    #   short -1 medium   → +1 x 3  =  +3 (w=3)
    #   long -3 weak      → -3 x 1  =  -3 (w=1)
    # Σcontrib = 31, Σw = 12 → true weighted mean = 31 / 12 ≈ 2.583
    assert row["weighted_pnl_pct"] == pytest.approx(31 / 12)
    # sort_score = (31/12) x 4 / (4 + 5) = 124 / 108 ≈ 1.148
    assert row["sort_score"] == pytest.approx((31 / 12) * 4 / 9)
    # TX committed.
    assert recompute_fixture.session_factory.sessions[0].commits == 1


async def test_cold_start_inserts_null_accuracy_and_pnl(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """Zero resolved outcomes → accuracy_pct + weighted_pnl_pct both None,
    sort_score=0.0 (NOT None — it's NOT NULL on the DB), and the row IS
    INSERTed. Preserves auditability: every recompute leaves a trail,
    even those that found nothing.
    """
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "b" * 32
    # No entry in rows_by_source → defaults to [].

    await _recompute_async(src_id)

    assert len(recompute_fixture.reputation_repo.snapshots) == 1
    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 0
    assert row["outcomes_correct"] == 0
    assert row["accuracy_pct"] is None
    assert row["weighted_pnl_pct"] is None
    assert row["sort_score"] == 0.0


async def test_all_correct_yields_100pct(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """5-of-5 correct → accuracy 100%. Also pins that a clean sweep on
    high-conviction bets produces a positive PnL signal."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "c" * 32
    recompute_fixture.outcome_repo.rows_by_source[src_id] = [
        _row(direction="long", conviction="strong", pct=4.0, is_correct=True),
        _row(direction="long", conviction="strong", pct=2.5, is_correct=True),
        _row(direction="short", conviction="medium", pct=-3.0, is_correct=True),
        _row(direction="long", conviction="medium", pct=1.0, is_correct=True),
        _row(direction="short", conviction="weak", pct=-2.0, is_correct=True),
    ]

    await _recompute_async(src_id)

    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 5
    assert row["outcomes_correct"] == 5
    assert row["accuracy_pct"] == pytest.approx(100.0)
    # Contributions: 4x5=20, 2.5x5=12.5, +3x3=9, 1x3=3, +2x1=2 → 46.5
    # Weights: 5, 5, 3, 3, 1 → Σw = 17
    # True weighted mean = 46.5 / 17 ≈ 2.7353
    assert row["weighted_pnl_pct"] == pytest.approx(46.5 / 17)
    # sort_score = (46.5/17) x 5 / 10 = 46.5 / 34 ≈ 1.3676
    assert row["sort_score"] == pytest.approx(46.5 / 34)


async def test_zero_correct_with_some_resolved_yields_zero_pct(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """0-of-3 correct → 0.0% accuracy (NOT ``None``). The cold-start
    signal is "no resolutions at all", not "you've been wrong every
    time". PnL is negative across these bad calls."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "d" * 32
    recompute_fixture.outcome_repo.rows_by_source[src_id] = [
        _row(direction="long", conviction="medium", pct=-2.0, is_correct=False),
        _row(direction="long", conviction="strong", pct=-4.0, is_correct=False),
        _row(direction="short", conviction="weak", pct=3.0, is_correct=False),
    ]

    await _recompute_async(src_id)

    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 3
    assert row["outcomes_correct"] == 0
    assert row["accuracy_pct"] == pytest.approx(0.0)
    # Contributions: -2x3=-6, -4x5=-20, -3x1=-3 → Σ=-29
    # Weights: 3, 5, 1 → Σw = 9
    # True weighted mean = -29 / 9 ≈ -3.222
    assert row["weighted_pnl_pct"] == pytest.approx(-29 / 9)
    # sort_score = (-29/9) x 3 / 8 = -29 / 24 ≈ -1.208
    assert row["sort_score"] == pytest.approx(-29 / 24)


async def test_flat_outcome_contributes_signed_value_to_pnl(
    recompute_fixture: _RecomputeFixture,
) -> None:
    """A "flat" outcome (|pct| < 0.5%) counts as resolved-but-not-correct
    for hit rate (wash_zero), but contributes its honest signed value
    to PnL (honest_signed). Pins both rules in one test."""
    from merlin_worker.jobs.recompute_source_reputation import _recompute_async

    src_id = "tg:" + "e" * 32
    recompute_fixture.outcome_repo.rows_by_source[src_id] = [
        # +0.3% long: below flat threshold → is_correct=False (wash_zero).
        # PnL contribution: +0.3 x 1 = +0.3 (honest_signed).
        _row(direction="long", conviction="weak", pct=0.3, is_correct=False),
        # Clear winner to balance the row.
        _row(direction="long", conviction="medium", pct=2.0, is_correct=True),
    ]

    await _recompute_async(src_id)

    row = recompute_fixture.reputation_repo.snapshots[0]
    assert row["outcomes_resolved"] == 2
    assert row["outcomes_correct"] == 1  # only the clear winner
    assert row["accuracy_pct"] == pytest.approx(50.0)
    # Contributions: +0.3 x 1 = +0.3 (w=1); +2.0 x 3 = +6.0 (w=3)
    # Σcontrib = 6.3, Σw = 4 → true weighted mean = 6.3 / 4 = 1.575
    assert row["weighted_pnl_pct"] == pytest.approx(1.575)
