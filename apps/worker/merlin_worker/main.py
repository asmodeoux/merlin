"""Entry point — boots an RQ worker against the configured queues.

Besides draining the queues, the worker hosts the recurring cron jobs
in a daemon thread (see ``merlin_worker.schedules.cron``): the Q-030
resolution sweep (re-fires bets whose RQ job was lost from Redis) and
the Q-031 first-call nudge (activation DMs). Each is gated by its own
``*_enabled`` flag; the thread starts iff at least one is registered.
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

    # Recurring crons (Q-030 sweep + Q-031 nudge) ride along in a daemon
    # thread. It must NOT be the worker's main loop — the worker owns
    # the main thread for forking + signal handling — and a daemon
    # thread dies with the process on shutdown, so there's nothing to
    # join. Start it only if at least one cron is enabled.
    scheduler = build_cron_scheduler(connection=connection, settings=settings)
    cron_jobs = scheduler.get_jobs()
    if cron_jobs:
        threading.Thread(
            target=run_cron_forever,
            args=(scheduler,),
            name="merlin-cron",
            daemon=True,
        ).start()
        _log.info(
            "cron thread started with %d job(s): %s",
            len(cron_jobs),
            ", ".join(job.func_name for job in cron_jobs),
        )

    queues = [Queue(name, connection=connection) for name in settings.rq_queues]
    worker = Worker(queues, connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    run()
