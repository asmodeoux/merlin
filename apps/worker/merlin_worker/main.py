"""Entry point — boots an RQ worker against the configured queues.

Besides draining the queues, the worker hosts the resolution-sweep
cron in a daemon thread (Q-030). The sweep is the durable backstop
that re-fires bets whose RQ resolution job was lost from Redis
(eviction / OOM / restart) — see ``merlin_worker.schedules.cron``.
"""

from __future__ import annotations

import logging
import threading

import redis
from rq import Queue, Worker

from merlin_worker.config import Settings
from merlin_worker.schedules.cron import build_cron_scheduler, run_cron_forever

_log = logging.getLogger("merlin.worker.main")


def run() -> None:
    """Start an RQ worker draining the configured queues."""
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper())

    connection = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]

    # Resolution-sweep cron rides along in a daemon thread (Q-030). It
    # must NOT be the worker's main loop — the worker owns the main
    # thread for forking + signal handling — and a daemon thread dies
    # with the process on shutdown, so there's nothing to join.
    if settings.resolution_sweep_enabled:
        scheduler = build_cron_scheduler(connection=connection, settings=settings)
        threading.Thread(
            target=run_cron_forever,
            args=(scheduler,),
            name="merlin-resolution-sweep",
            daemon=True,
        ).start()
        _log.info(
            "resolution-sweep cron enabled (interval=%ss, grace=%ss, max_batch=%s)",
            settings.resolution_sweep_interval_s,
            settings.resolution_sweep_grace_s,
            settings.resolution_sweep_max_batch,
        )

    queues = [Queue(name, connection=connection) for name in settings.rq_queues]
    worker = Worker(queues, connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    run()
