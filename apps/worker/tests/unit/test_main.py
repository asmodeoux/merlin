"""Unit tests for the worker entry point ``run()``.

We fake every boundary (``redis.from_url``, the cron builder + loop,
``Queue`` / ``Worker``, and ``threading.Thread``) so the test asserts
the *wiring* without opening a connection or forking a worker:

* When the built scheduler has at least one cron job, ``run()`` starts
  it in a daemon thread BEFORE the worker drains.
* When the scheduler has no jobs (all crons disabled), no thread starts.
* Either way the worker drains the configured queues with the
  scheduler component on.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest


class _FakeScheduler:
    def __init__(self, jobs: list[Any]) -> None:
        self._jobs = jobs

    def get_jobs(self) -> list[Any]:
        return self._jobs


class _FakeJob:
    def __init__(self, func_name: str) -> None:
        self.func_name = func_name


class _FakeThread:
    instances: ClassVar[list[_FakeThread]] = []

    def __init__(self, *, target: Any, args: Any, name: str, daemon: bool) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        _FakeThread.instances.append(self)

    def start(self) -> None:
        self.started = True


class _FakeWorker:
    last: ClassVar[_FakeWorker | None] = None

    def __init__(self, queues: Any, connection: Any) -> None:
        self.queues = queues
        self.connection = connection
        self.with_scheduler: bool | None = None
        _FakeWorker.last = self

    def work(self, with_scheduler: bool = False) -> None:
        self.with_scheduler = with_scheduler


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    _FakeThread.instances = []
    _FakeWorker.last = None


def _wire(monkeypatch: pytest.MonkeyPatch, *, jobs: list[Any]) -> _FakeScheduler:
    scheduler = _FakeScheduler(jobs)
    monkeypatch.setattr("merlin_worker.main.redis.from_url", lambda _url: "CONN")
    monkeypatch.setattr(
        "merlin_worker.main.build_cron_scheduler",
        lambda *, connection, settings: scheduler,
    )
    monkeypatch.setattr("merlin_worker.main.run_cron_forever", lambda _s: None)
    monkeypatch.setattr("merlin_worker.main.Queue", lambda name, connection: ("Q", name))
    monkeypatch.setattr("merlin_worker.main.Worker", _FakeWorker)
    monkeypatch.setattr("merlin_worker.main.threading.Thread", _FakeThread)
    return scheduler


def test_run_starts_cron_thread_when_jobs_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _wire(monkeypatch, jobs=[_FakeJob("merlin_worker.jobs.x")])

    from merlin_worker.main import run

    run()

    # Exactly one daemon thread, started, driving the cron loop with the
    # scheduler we built.
    assert len(_FakeThread.instances) == 1
    thread = _FakeThread.instances[0]
    assert thread.started is True
    assert thread.daemon is True
    assert thread.args == (scheduler,)
    # Worker drained with the scheduler component on (handles enqueue_at).
    assert _FakeWorker.last is not None
    assert _FakeWorker.last.with_scheduler is True


def test_run_skips_cron_thread_when_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, jobs=[])

    from merlin_worker.main import run

    run()

    assert _FakeThread.instances == []
    assert _FakeWorker.last is not None
    assert _FakeWorker.last.with_scheduler is True
