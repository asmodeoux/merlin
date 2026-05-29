"""Unit tests for ``FakeJobQueue``."""

from __future__ import annotations

from datetime import UTC, datetime

from merlin_infrastructure.queue.fake import FakeJobQueue


def test_enqueue_at_records_call() -> None:
    # Job id format is ``resolve-<uuid>`` — RQ's ``validate_job_id``
    # rejects colons (Sprint-1 live regression). Pin the dash format
    # here so the fake stays compatible with the real adapter.
    q = FakeJobQueue()
    when = datetime(2026, 6, 12, tzinfo=UTC)
    job_id = q.enqueue_at(
        when,
        "merlin_worker.jobs.resolve_outcome",
        "claim-1",
        job_id="resolve-claim-1",
    )
    assert job_id == "resolve-claim-1"
    assert len(q.calls) == 1
    call = q.calls[0]
    assert call.when == when
    assert call.fn_path == "merlin_worker.jobs.resolve_outcome"
    assert call.args == ("claim-1",)
    assert call.job_id == "resolve-claim-1"


def test_enqueue_records_immediate_call_without_when() -> None:
    q = FakeJobQueue()
    job_id = q.enqueue(
        "merlin_worker.jobs.recompute_source_reputation",
        "src-1",
    )
    assert job_id.startswith("fake-")
    assert q.calls[0].when is None
    assert q.calls[0].args == ("src-1",)


def test_cancel_returns_true_for_known_job() -> None:
    q = FakeJobQueue()
    job_id = q.enqueue("foo", job_id="known")
    assert q.cancel(job_id) is True
    assert "known" in q.cancelled


def test_cancel_returns_false_for_unknown_job() -> None:
    q = FakeJobQueue()
    assert q.cancel("never-scheduled") is False
    assert q.cancelled == set()


def test_enqueue_auto_assigns_unique_ids_when_omitted() -> None:
    q = FakeJobQueue()
    ids = {q.enqueue("foo") for _ in range(5)}
    assert len(ids) == 5
