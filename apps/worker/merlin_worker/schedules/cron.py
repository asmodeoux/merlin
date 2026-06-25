"""RQ ``CronScheduler`` wiring — the durable resolution sweep (Q-030).

This is the "Sprint-2 cron primitive" the ``schedules`` package
docstring anticipated. It registers exactly one recurring job —
``sweep_unresolved_claims`` — on a fixed interval, the reconciliation
backstop that re-fires any bet whose RQ resolution job was lost from
Redis (eviction / OOM / restart).

Driver note — why we don't call ``CronScheduler.start()``:
``start()`` installs ``SIGINT`` / ``SIGTERM`` handlers via
``signal.signal``, which Python only permits on the main thread. The
worker owns the main thread (``Worker.work()`` needs it for its own
signal handling + work-horse forking), so the cron has to ride along
in a daemon thread. :func:`run_cron_forever` reproduces ``start()``'s
enqueue loop (``enqueue_jobs`` + interval sleep) without the signal
handlers the worker already owns.

Interval jobs that have never run fire immediately on the first loop
(RQ's ``CronJob.should_run`` returns True when ``latest_enqueue_time``
is unset), so a worker restart doubles as a free catch-up sweep after
any outage.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from merlin_infrastructure.queue.names import DEFAULT_QUEUE_NAME
from rq.cron import CronScheduler

if TYPE_CHECKING:
    from redis import Redis

    from merlin_worker.config import Settings

_log = logging.getLogger("merlin.worker.schedules.cron")

# Floor on the cron loop's sleep so a misconfigured tiny interval can't
# spin the daemon thread into a busy-loop hammering Redis.
_MIN_SLEEP_SECONDS = 1.0


def build_cron_scheduler(*, connection: Redis, settings: Settings) -> CronScheduler:
    """Build a ``CronScheduler`` with the resolution sweep registered.

    The sweep job runs on the ``default`` queue (the worker already
    drains it); the sweep itself re-enqueues ``resolve_outcome`` onto
    the ``resolution`` queue. ``job_timeout`` mirrors the worker's
    default so a slow DB scan doesn't trip RQ's timeout.
    """
    # Lazy import: keeps the heavy ``jobs`` import graph (SQLAlchemy etc.)
    # off the module-import path and mirrors the worker's lazy-import
    # discipline.
    from merlin_worker.jobs import sweep_unresolved_claims

    scheduler = CronScheduler(connection=connection, name="merlin-resolution-sweep")
    scheduler.register(
        sweep_unresolved_claims,
        queue_name=DEFAULT_QUEUE_NAME,
        interval=settings.resolution_sweep_interval_s,
        job_timeout=settings.rq_default_timeout_s,
    )
    return scheduler


def run_cron_forever(scheduler: CronScheduler) -> None:
    """Drive ``scheduler``'s enqueue loop forever. Run in a daemon thread.

    A transient Redis blip on one tick is logged and retried on the
    next cycle rather than killing the thread — losing the sweeper
    silently would reintroduce the very gap Q-030 closes.
    """
    _log.info("resolution-sweep cron loop starting (interval-driven)")
    while True:
        try:
            scheduler.enqueue_jobs()
        except Exception:
            _log.exception(
                "resolution-sweep cron tick failed; retrying next cycle",
            )
        sleep_seconds = max(scheduler.calculate_sleep_interval(), _MIN_SLEEP_SECONDS)
        time.sleep(sleep_seconds)
