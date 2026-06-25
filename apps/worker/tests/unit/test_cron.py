"""Unit tests for the resolution-sweep cron wiring (Q-030).

``build_cron_scheduler`` is exercised against a real RQ ``CronScheduler``
(its ``__init__`` + ``register`` do no Redis I/O — a MagicMock connection
suffices). ``run_cron_forever`` is driven with a fake scheduler and a
``time.sleep`` that breaks the otherwise-infinite loop after one tick.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from merlin_worker.config import Settings
from merlin_worker.schedules import cron as cron_mod


def test_build_cron_scheduler_registers_the_sweep_on_default_queue() -> None:
    settings = Settings(environment="test", sentry_dsn="", resolution_sweep_interval_s=123)
    scheduler = cron_mod.build_cron_scheduler(connection=MagicMock(), settings=settings)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    # RQ enqueues by import path; the func must resolve to the sweep job.
    assert job.func_name == "merlin_worker.jobs.sweep_unresolved.sweep_unresolved_claims"
    # Sweep runs on the queue the worker already drains; it re-enqueues
    # resolve_outcome onto the resolution queue itself.
    assert job.queue_name == "default"
    assert job.interval == 123


def test_run_cron_forever_enqueues_then_sleeps_at_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {"enqueue": 0, "sleep": []}

    class FakeScheduler:
        def enqueue_jobs(self) -> list[Any]:
            calls["enqueue"] += 1
            return []

        def calculate_sleep_interval(self) -> float:
            return 0.0

    def fake_sleep(seconds: float) -> None:
        calls["sleep"].append(seconds)
        raise KeyboardInterrupt  # break the infinite loop after one tick

    monkeypatch.setattr(cron_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cron_mod.run_cron_forever(FakeScheduler())

    assert calls["enqueue"] == 1
    # A 0s computed interval is floored to _MIN_SLEEP_SECONDS so the
    # daemon thread can't busy-spin on Redis.
    assert calls["sleep"] == [cron_mod._MIN_SLEEP_SECONDS]


def test_run_cron_forever_swallows_tick_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeScheduler:
        def __init__(self) -> None:
            self.ticks = 0

        def enqueue_jobs(self) -> list[Any]:
            self.ticks += 1
            raise RuntimeError("transient redis blip")

        def calculate_sleep_interval(self) -> float:
            return 5.0

    def fake_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cron_mod.time, "sleep", fake_sleep)

    sched = FakeScheduler()
    # A blip on one tick must NOT kill the loop — losing the sweeper
    # silently would reintroduce the very gap Q-030 closes. The error is
    # logged and the loop proceeds to sleep (here interrupted).
    with pytest.raises(KeyboardInterrupt):
        cron_mod.run_cron_forever(sched)
    assert sched.ticks == 1
