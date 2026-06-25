"""Unit tests for the operator recovery CLI (Q-030).

``RQQueueAdapter`` is monkeypatched so ``rerun`` never touches Redis;
``run_sweep`` is stubbed so ``backfill`` never touches Postgres. We pin
the CLI contract the RUNBOOK + Makefile targets depend on.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest


class _FakeQueue:
    """Records enqueue calls; constructed by the CLI in place of RQ."""

    last_queue_name: ClassVar[str | None] = None
    calls: ClassVar[list[tuple[str, tuple[Any, ...], str | None]]] = []

    def __init__(self, *, redis: Any, queue_name: str) -> None:
        _FakeQueue.last_queue_name = queue_name

    def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
        _FakeQueue.calls.append((fn_path, args, job_id))
        return job_id or "x"


@pytest.fixture(autouse=True)
def _reset_fake_queue() -> None:
    _FakeQueue.calls = []
    _FakeQueue.last_queue_name = None


def test_rerun_enqueues_resolve_with_deterministic_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("merlin_infrastructure.queue.rq.RQQueueAdapter", _FakeQueue)
    from merlin_worker import admin

    claim_id = str(uuid4())
    job_id = admin._rerun(claim_id)

    assert job_id == f"resolve-{claim_id}"
    assert _FakeQueue.last_queue_name == "resolution"
    assert _FakeQueue.calls == [
        ("merlin_worker.jobs.resolve_outcome", (claim_id,), f"resolve-{claim_id}")
    ]


def test_rerun_rejects_a_non_uuid_claim_id() -> None:
    from merlin_worker import admin

    with pytest.raises(SystemExit):
        admin._rerun("not-a-uuid")


def test_backfill_runs_the_sweep_and_returns_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run_sweep(
        *, grace_seconds: int | None = None, max_batch: int | None = None
    ) -> int:
        seen["grace"] = grace_seconds
        return 4

    monkeypatch.setattr("merlin_worker.jobs.sweep_unresolved.run_sweep", fake_run_sweep)
    from merlin_worker import admin

    assert admin._backfill(grace_seconds=0) == 4
    assert seen["grace"] == 0


def test_main_dispatches_rerun(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("merlin_infrastructure.queue.rq.RQQueueAdapter", _FakeQueue)
    from merlin_worker import admin

    claim_id = str(uuid4())
    assert admin.main(["rerun", claim_id]) == 0
    assert claim_id in capsys.readouterr().out
    assert _FakeQueue.calls and _FakeQueue.calls[0][2] == f"resolve-{claim_id}"


def test_main_dispatches_backfill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run_sweep(**_kwargs: Any) -> int:
        return 2

    monkeypatch.setattr("merlin_worker.jobs.sweep_unresolved.run_sweep", fake_run_sweep)
    from merlin_worker import admin

    assert admin.main(["backfill"]) == 0
    assert "re-enqueued 2" in capsys.readouterr().out
