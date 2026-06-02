"""RQ-backed ``JobQueuePort`` adapter.

Wraps ``rq.Queue`` and uses its native ``enqueue_at`` for scheduled
dispatch. No separate ``rq-scheduler`` dependency at v0 — the modern
RQ release embeds scheduling.

Per plan §3:

- ``job_id`` is deterministic (``f"resolve-{claim_id}"``,
  ``f"reputation-{source_id}"``). Lets ``cancel(job_id)`` and the
  ``/admin/rerun`` path work with natural keys. RQ's
  ``validate_job_id`` rejects characters outside ``[A-Za-z0-9_-]``,
  so use a dash separator — colons raise ``ValueError`` at
  ``enqueue_at`` time (Sprint-1 live regression).
- Retry policy: ``Retry(max=3, interval=[60, 300, 900])`` — 1m / 5m /
  15m. Wired into ``enqueue_at`` so deferred resolution jobs get the
  same protection as immediate ones.
- ``cancel()`` is best-effort: returns True if a job was cancelled,
  False if it had already fired or didn't exist.

Queue-name policy (post Sprint-1 ``"merlin"`` mismatch bug):

* ``queue_name`` is a **required** keyword argument. Callers must
  reach for one of the constants in
  ``merlin_infrastructure.queue.names`` (``RESOLUTION_QUEUE_NAME`` for
  the API's enqueue, ``REPUTATION_QUEUE_NAME`` for the resolver's
  follow-up). Letting the adapter silently default to ``"merlin"``
  caused the live "I never got my outcome message" bug — RQ happily
  parked the job on a queue the worker wasn't draining.
* The preflight suite cross-checks the names against
  ``Settings.rq_queues`` so a future drift fails CI long before it
  reaches a user.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job

if TYPE_CHECKING:
    from redis import Redis

_log = logging.getLogger("merlin.infrastructure.queue.rq")

# Match plan §3 retry policy. Tuned for CoinGecko Demo's failure modes:
# rate-limit recoveries land within seconds; transient 5xxs typically
# clear inside 5 minutes; the 15-minute final retry catches longer
# upstream incidents before we surface a "couldn't be settled" message.
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_INTERVALS = [60, 300, 900]


class RQQueueAdapter:
    """Concrete ``JobQueuePort`` using ``rq.Queue.enqueue_at``."""

    def __init__(
        self,
        *,
        redis: Redis,
        queue_name: str,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_intervals: list[int] | None = None,
    ) -> None:
        """Build an RQ ``Queue`` pinned to ``redis`` for both enqueue paths.

        ``queue_name`` is required — pass one of the constants from
        ``merlin_infrastructure.queue.names`` to keep the API enqueue
        path, the resolver's follow-up enqueue, and the worker's
        drained-queues list aligned. The silent-default behaviour of
        prior revisions caused the Sprint-1 ``"merlin"`` mismatch
        regression where the worker drained queues the API never
        touched.
        """
        if not queue_name:
            raise ValueError(
                "RQQueueAdapter.queue_name must be a non-empty string — "
                "pass one of merlin_infrastructure.queue.names."
            )
        self._redis = redis
        self._queue_name = queue_name
        self._queue = Queue(queue_name, connection=redis)
        self._retry_max = retry_max
        self._retry_intervals = retry_intervals or DEFAULT_RETRY_INTERVALS

    @property
    def queue_name(self) -> str:
        """Return the queue name passed at construction time.

        Preflight tests + ``/admin`` diagnostics read this to verify
        wiring without poking at private state.
        """
        return self._queue_name

    def enqueue_at(
        self,
        when: datetime,
        fn_path: str,
        *args: Any,
        job_id: str | None = None,
    ) -> str:
        """Schedule ``fn_path(*args)`` to fire at ``when``."""
        job = self._queue.enqueue_at(
            when,
            fn_path,
            *args,
            job_id=job_id,
            retry=Retry(max=self._retry_max, interval=self._retry_intervals),
        )
        return str(job.id)

    def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
        """Enqueue ``fn_path(*args)`` for next-available execution."""
        job = self._queue.enqueue(
            fn_path,
            *args,
            job_id=job_id,
            retry=Retry(max=self._retry_max, interval=self._retry_intervals),
        )
        return str(job.id)

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending scheduled job. Returns True iff a job was cancelled."""
        try:
            job = Job.fetch(job_id, connection=self._redis)
        except NoSuchJobError:
            return False
        if job.is_finished or job.is_failed:
            return False
        job.cancel()
        return True
