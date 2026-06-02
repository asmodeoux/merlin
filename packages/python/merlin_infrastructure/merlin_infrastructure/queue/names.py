"""Canonical RQ queue names.

One module so the API enqueue path, the resolver's follow-up enqueue,
and the worker boot list all reach for the same identifiers. A
preflight test asserts that what the API enqueues on is exactly what
the worker drains — the kind of cross-cutting wiring fault that has
already cost us one live bug (the "merlin" default queue mismatch).

Sprint-1 live regression we want to catch in CI forever:

* ``POST /claims`` enqueued ``resolve_outcome`` on the queue named
  ``"merlin"`` (the historical ``RQQueueAdapter`` default).
* The worker drained ``["default", "resolution", "reputation"]`` per
  ``apps/worker/.env.example``.
* Result: the job sat in Redis under ``merlin`` until a human noticed
  the user never got an outcome message.

Constants below are intentionally narrow — no enum, no aliases. A
string is the only thing RQ accepts, and a single import site keeps
``ruff``-style ``hardcoded-string`` lint surface honest.
"""

from __future__ import annotations

from typing import Final

# Catch-all bucket for ad-hoc jobs that don't fit the per-purpose
# queues. The worker drains this so admin reruns + future utility jobs
# don't need a separate worker.
DEFAULT_QUEUE_NAME: Final[str] = "default"

# ``POST /claims`` enqueues ``merlin_worker.jobs.resolve_outcome`` here
# via the JobQueuePort adapter. Worker MUST drain this queue.
RESOLUTION_QUEUE_NAME: Final[str] = "resolution"

# ``resolve_outcome`` enqueues ``recompute_source_reputation`` here as
# its reactive follow-up (Q-029 — no daily cron). Worker MUST drain
# this queue too.
REPUTATION_QUEUE_NAME: Final[str] = "reputation"

# The single list every component should reach for instead of
# hard-coding the membership in two places. Preflight tests cross-check
# this list against:
#   * ``apps/worker/merlin_worker/config.Settings.rq_queues`` default
#   * the queue name the API + resolver pass to ``RQQueueAdapter``
ALL_QUEUE_NAMES: Final[tuple[str, ...]] = (
    DEFAULT_QUEUE_NAME,
    RESOLUTION_QUEUE_NAME,
    REPUTATION_QUEUE_NAME,
)
