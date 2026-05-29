"""Job-queue adapters — concrete implementations of ``JobQueuePort``.

The whole passive-worker design (plan §3) flows through this package.
The API enqueues at claim-commit time; the worker pulls jobs at their
scheduled time; no table ever gets scanned for "what's due".

Sub-modules:

- ``rq`` — production adapter wrapping ``rq.Queue`` and its built-in
  ``enqueue_at`` scheduler primitives. Deterministic ``job_id``
  conventions (``resolve:{claim_id}``, ``reputation:{source_id}``).
- ``fake`` — in-memory test adapter; records calls without
  dispatching. Sync-mode RQ for integration tests lives in
  ``apps/worker/tests/conftest.py``.
"""

from merlin_infrastructure.queue.fake import FakeJobQueue
from merlin_infrastructure.queue.rq import RQQueueAdapter

__all__ = ["FakeJobQueue", "RQQueueAdapter"]
