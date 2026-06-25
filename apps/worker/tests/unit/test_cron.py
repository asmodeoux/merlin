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

_SWEEP_FN = "merlin_worker.jobs.sweep_unresolved.sweep_unresolved_claims"
_NUDGE_FN = "merlin_worker.jobs.first_call_nudge.send_first_call_nudges"


def test_build_cron_scheduler_registers_both_jobs_when_enabled() -> None:
    settings = Settings(
        environment="test",
        sentry_dsn="",
        resolution_sweep_interval_s=123,
        first_call_nudge_interval_s=456,
    )
    scheduler = cron_mod.build_cron_scheduler(connection=MagicMock(), settings=settings)

    jobs = {job.func_name: job for job in scheduler.get_jobs()}
    assert set(jobs) == {_SWEEP_FN, _NUDGE_FN}
    # Both run on the queue the worker already drains; each carries its
    # own configured interval.
    assert jobs[_SWEEP_FN].queue_name == "default"
    assert jobs[_SWEEP_FN].interval == 123
    assert jobs[_NUDGE_FN].queue_name == "default"
    assert jobs[_NUDGE_FN].interval == 456


def test_build_cron_scheduler_registers_only_enabled_jobs() -> None:
    sweep_only = cron_mod.build_cron_scheduler(
        connection=MagicMock(),
        settings=Settings(
            environment="test",
            sentry_dsn="",
            first_call_nudge_enabled=False,
        ),
    )
    assert [j.func_name for j in sweep_only.get_jobs()] == [_SWEEP_FN]

    nudge_only = cron_mod.build_cron_scheduler(
        connection=MagicMock(),
        settings=Settings(
            environment="test",
            sentry_dsn="",
            resolution_sweep_enabled=False,
        ),
    )
    assert [j.func_name for j in nudge_only.get_jobs()] == [_NUDGE_FN]


def test_build_cron_scheduler_registers_nothing_when_all_disabled() -> None:
    scheduler = cron_mod.build_cron_scheduler(
        connection=MagicMock(),
        settings=Settings(
            environment="test",
            sentry_dsn="",
            resolution_sweep_enabled=False,
            first_call_nudge_enabled=False,
        ),
    )
    assert scheduler.get_jobs() == []


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
