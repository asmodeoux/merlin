"""Unit tests for the worker entry point ``run()``.

We fake every boundary (``redis.from_url``, the cron builder + loop,
``Queue`` / ``Worker``, and ``threading.Thread``) so the test asserts
the *wiring* without opening a connection or forking a worker:

* When the sweep is enabled (default), ``run()`` builds the cron
  scheduler and starts it in a daemon thread BEFORE the worker drains.
* When disabled, no thread is started.
* Either way the worker drains the configured queues with the
  scheduler component on.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest


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


def _wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("merlin_worker.main.redis.from_url", lambda _url: "CONN")
    monkeypatch.setattr(
        "merlin_worker.main.build_cron_scheduler",
        lambda *, connection, settings: seen.setdefault("scheduler", object()),
    )
    monkeypatch.setattr("merlin_worker.main.run_cron_forever", lambda _s: None)
    monkeypatch.setattr("merlin_worker.main.Queue", lambda name, connection: ("Q", name))
    monkeypatch.setattr("merlin_worker.main.Worker", _FakeWorker)
    monkeypatch.setattr("merlin_worker.main.threading.Thread", _FakeThread)
    return seen


def test_run_starts_sweep_thread_and_drains_with_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESOLUTION_SWEEP_ENABLED", "true")
    seen = _wire(monkeypatch)

    from merlin_worker.main import run

    run()

    # Exactly one daemon thread, started, driving the cron loop with the
    # scheduler we built.
    assert len(_FakeThread.instances) == 1
    thread = _FakeThread.instances[0]
    assert thread.started is True
    assert thread.daemon is True
    assert thread.args == (seen["scheduler"],)
    # Worker drained with the scheduler component on (handles enqueue_at).
    assert _FakeWorker.last is not None
    assert _FakeWorker.last.with_scheduler is True


def test_run_skips_sweep_thread_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESOLUTION_SWEEP_ENABLED", "false")
    _wire(monkeypatch)

    from merlin_worker.main import run

    run()

    assert _FakeThread.instances == []
    assert _FakeWorker.last is not None
    assert _FakeWorker.last.with_scheduler is True
