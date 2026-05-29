"""In-memory ``JobQueuePort`` for tests.

Records enqueue calls without dispatching. Lets unit + API integration
tests assert "the queue was called with (t_resolve, resolve_outcome,
claim_id)" without spinning up Redis.

Worker integration tests use sync-mode RQ instead (real Queue,
``is_async=False``) so the resolver actually runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EnqueueCall:
    """One captured enqueue invocation."""

    when: datetime | None
    fn_path: str
    args: tuple[Any, ...]
    job_id: str | None


@dataclass
class FakeJobQueue:
    """Captures enqueue calls; pretends to schedule."""

    calls: list[EnqueueCall] = field(default_factory=list)
    cancelled: set[str] = field(default_factory=set)
    _counter: int = 0

    def enqueue_at(
        self,
        when: datetime,
        fn_path: str,
        *args: Any,
        job_id: str | None = None,
    ) -> str:
        """Record a scheduled enqueue and return the resolved job id."""
        self._counter += 1
        resolved = job_id or f"fake-{self._counter}"
        self.calls.append(EnqueueCall(when=when, fn_path=fn_path, args=args, job_id=resolved))
        return resolved

    def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
        """Record an immediate enqueue and return the resolved job id."""
        self._counter += 1
        resolved = job_id or f"fake-{self._counter}"
        self.calls.append(EnqueueCall(when=None, fn_path=fn_path, args=args, job_id=resolved))
        return resolved

    def cancel(self, job_id: str) -> bool:
        """Mark ``job_id`` cancelled and remove it from the call log."""
        for call in self.calls:
            if call.job_id == job_id:
                self.cancelled.add(job_id)
                return True
        return False
