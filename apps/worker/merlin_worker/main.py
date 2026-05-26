"""Entry point — boots an RQ worker against the configured queues."""

from __future__ import annotations

import logging

import redis
from rq import Queue, Worker

from merlin_worker.config import Settings


def run() -> None:
    """Start an RQ worker draining the configured queues."""
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper())

    connection = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    queues = [Queue(name, connection=connection) for name in settings.rq_queues]
    worker = Worker(queues, connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    run()
