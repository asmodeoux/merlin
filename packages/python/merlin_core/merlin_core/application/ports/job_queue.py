"""JobQueuePort — schedule deferred work.

The whole point of the passive worker design (principle
#2): we never poll an "events to resolve" table. Instead, when a claim
commits we call ``enqueue_at(t_resolve, "merlin_worker.jobs.resolve_outcome",
claim_id)`` and forget about it. RQ Scheduler fires at the wall-clock
time. No scans.

Implemented by ``merlin_infrastructure.queue.rq.RQQueueAdapter`` (real
Redis + RQ) and ``merlin_infrastructure.queue.fake.FakeQueue`` (tests —
records calls without dispatching).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class JobQueuePort(Protocol):
    """Schedule deferred or immediate jobs against a backing queue."""

    def enqueue_at(
        self,
        when: datetime,
        fn_path: str,
        *args: Any,
        job_id: str | None = None,
    ) -> str:
        """Schedule ``fn_path(*args)`` to fire at ``when``.

        Args:
            when: Wall-clock time at which the job should run. Must be
                timezone-aware (UTC). RQ Scheduler holds the schedule
                in Redis; no Postgres-level cron required.
            fn_path: Importable dotted path of the function to call,
                e.g. ``"merlin_worker.jobs.resolve_outcome"``. Resolved
                lazily by the worker — the API enqueues without
                importing the worker code.
            *args: Positional arguments forwarded to ``fn_path``. Must
                be JSON-serializable (RQ uses pickle by default but we
                discipline to JSON-safe values for legibility).
            job_id: Optional deterministic job id. Defaults to a UUID;
                pass e.g. ``f"resolve-{claim_id}"`` for natural keys so
                ``cancel(job_id)`` is straightforward. NOTE: RQ's
                ``validate_job_id`` only accepts ``[A-Za-z0-9_-]`` —
                use ``-`` (not ``:``) as the separator or the
                downstream ``enqueue_at`` raises ``ValueError``.

        Returns:
            The resolved job id (the one passed in, or the generated UUID).
        """
        ...

    def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
        """Enqueue ``fn_path(*args)`` for immediate (next-available) execution.

        Used by the resolver to dispatch ``recompute_source_reputation``
        the moment an outcome lands — reactive reputation per Q-029.
        """
        ...

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending scheduled job. Returns True if a job was cancelled.

        Used by ``/admin/cancel`` flows and by the operator-facing rerun
        path. No-op (returns False) if the job already fired or doesn't
        exist.
        """
        ...
