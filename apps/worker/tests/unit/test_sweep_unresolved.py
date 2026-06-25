"""Unit tests for ``sweep_unresolved_claims`` — the Q-030 backstop.

The sweep's collaborators (DB session factory, ``ClaimRepository``,
``RQQueueAdapter``) are monkeypatched on their defining modules so the
test never opens a real Postgres / Redis. We pin the orchestration:

1. Each overdue claim id is re-enqueued as ``resolve_outcome`` with the
   deterministic ``resolve-{id}`` job id on the ``resolution`` queue.
2. Nothing overdue → returns 0 and never even constructs the queue.
3. ``grace_seconds`` / ``max_batch`` overrides reach the repo query
   (the ``make admin-backfill --grace-seconds`` path).
4. The sync ``sweep_unresolved_claims()`` entry bridges to ``run_sweep``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest


class _FakeSession:
    """Async-context-manager session that does nothing."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``get_session_factory(dsn)()`` yield a no-op ``_FakeSession``."""

    def _session_factory() -> _FakeSession:
        return _FakeSession()

    def fake_get_session_factory(_dsn: str) -> Any:
        return _session_factory

    monkeypatch.setattr(
        "merlin_infrastructure.db.session.get_session_factory",
        fake_get_session_factory,
    )


async def test_run_sweep_reenqueues_each_overdue_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [uuid4(), uuid4(), uuid4()]
    captured: dict[str, Any] = {}
    enqueued: list[tuple[str, tuple[Any, ...], str | None]] = []

    class FakeRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def list_unresolved_overdue(self, *, grace_seconds: int, limit: int) -> list[Any]:
            captured["grace"] = grace_seconds
            captured["limit"] = limit
            return ids

    class FakeQueue:
        def __init__(self, *, redis: Any, queue_name: str) -> None:
            captured["queue_name"] = queue_name

        def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
            enqueued.append((fn_path, args, job_id))
            return job_id or "x"

    _patch_session_factory(monkeypatch)
    monkeypatch.setattr("merlin_infrastructure.db.repositories.ClaimRepository", FakeRepo)
    monkeypatch.setattr("merlin_infrastructure.queue.rq.RQQueueAdapter", FakeQueue)

    from merlin_worker.jobs.sweep_unresolved import run_sweep

    count = await run_sweep()

    assert count == 3
    # Re-enqueues onto the resolution queue the worker drains + the API
    # enqueues on (the Sprint-1 queue-mismatch regression guard).
    assert captured["queue_name"] == "resolution"
    assert [fn for fn, _a, _j in enqueued] == ["merlin_worker.jobs.resolve_outcome"] * 3
    # Deterministic resolve-{id} job id so a second sweep dedupes onto
    # the same job rather than piling up duplicates.
    assert [job_id for _fn, _a, job_id in enqueued] == [f"resolve-{cid}" for cid in ids]
    assert [args for _fn, args, _j in enqueued] == [(str(cid),) for cid in ids]


async def test_run_sweep_noop_when_nothing_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def list_unresolved_overdue(self, *, grace_seconds: int, limit: int) -> list[Any]:
            return []

    class BoomQueue:
        def __init__(self, *, redis: Any, queue_name: str) -> None:
            raise AssertionError("queue must not be constructed when nothing is overdue")

    _patch_session_factory(monkeypatch)
    monkeypatch.setattr("merlin_infrastructure.db.repositories.ClaimRepository", FakeRepo)
    monkeypatch.setattr("merlin_infrastructure.queue.rq.RQQueueAdapter", BoomQueue)

    from merlin_worker.jobs.sweep_unresolved import run_sweep

    assert await run_sweep() == 0


async def test_run_sweep_forwards_grace_and_batch_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def list_unresolved_overdue(self, *, grace_seconds: int, limit: int) -> list[Any]:
            captured["grace"] = grace_seconds
            captured["limit"] = limit
            return []

    _patch_session_factory(monkeypatch)
    monkeypatch.setattr("merlin_infrastructure.db.repositories.ClaimRepository", FakeRepo)

    from merlin_worker.jobs.sweep_unresolved import run_sweep

    await run_sweep(grace_seconds=0, max_batch=7)

    assert captured == {"grace": 0, "limit": 7}


def test_sweep_unresolved_claims_sync_entry_bridges_to_run_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_sweep(**_kwargs: Any) -> int:
        return 5

    monkeypatch.setattr("merlin_worker.jobs.sweep_unresolved.run_sweep", fake_run_sweep)

    from merlin_worker.jobs.sweep_unresolved import sweep_unresolved_claims

    assert sweep_unresolved_claims() == 5
