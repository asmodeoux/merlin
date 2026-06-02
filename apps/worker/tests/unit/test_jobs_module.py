"""Unit smoke for worker job modules.

The full resolution sequence is exercised by ``apps/worker/tests/integration``
once the DB models land. This file guards the import path, signature
shape, and the dispatch helpers we already ship in Sprint 1.
"""

from __future__ import annotations


def test_jobs_module_exports_both_jobs() -> None:
    from merlin_worker import jobs

    assert hasattr(jobs, "resolve_outcome")
    assert hasattr(jobs, "recompute_source_reputation")
    assert callable(jobs.resolve_outcome)
    assert callable(jobs.recompute_source_reputation)


def test_schedules_module_is_intentionally_empty() -> None:
    """Q-029 invariant — no scheduled jobs at v0."""
    from merlin_worker import schedules

    # The schedules package is a documentation stub. It must export
    # nothing other than the docstring.
    public = [name for name in dir(schedules) if not name.startswith("_")]
    assert public == []


def test_resolve_outcome_signature() -> None:
    import inspect

    from merlin_worker.jobs.resolve_outcome import resolve_outcome

    sig = inspect.signature(resolve_outcome)
    assert list(sig.parameters) == ["claim_id"]


def test_recompute_source_reputation_signature() -> None:
    import inspect

    from merlin_worker.jobs.recompute_source_reputation import (
        recompute_source_reputation,
    )

    sig = inspect.signature(recompute_source_reputation)
    assert list(sig.parameters) == ["source_id"]


def test_config_settings_smoke() -> None:
    from merlin_infrastructure.queue.names import ALL_QUEUE_NAMES
    from merlin_worker.config import Settings

    s = Settings()
    assert s.environment in {"dev", "staging", "prod", "test"}
    # Default ``rq_queues`` MUST match ``ALL_QUEUE_NAMES`` so the
    # worker drains exactly the queues the API + resolver enqueue
    # on. Sprint-1 mismatch regression sits here.
    assert tuple(s.rq_queues) == ALL_QUEUE_NAMES
    assert s.coingecko_base_url.startswith("http")
