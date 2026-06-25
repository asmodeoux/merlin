"""Unit smoke for worker job modules.

The full resolution sequence is exercised by ``apps/worker/tests/integration``
once the DB models land. This file guards the import path, signature
shape, and the dispatch helpers we already ship in Sprint 1.
"""

from __future__ import annotations


def test_jobs_module_exports_all_jobs() -> None:
    from merlin_worker import jobs

    assert hasattr(jobs, "resolve_outcome")
    assert hasattr(jobs, "recompute_source_reputation")
    assert hasattr(jobs, "sweep_unresolved_claims")
    assert callable(jobs.resolve_outcome)
    assert callable(jobs.recompute_source_reputation)
    assert callable(jobs.sweep_unresolved_claims)


def test_schedules_exposes_the_resolution_sweep_cron() -> None:
    """Q-030 — the schedules package now hosts exactly one cron primitive.

    The historical Q-029 "intentionally empty" invariant was reversed by
    ADR (v): RQ-only scheduling can lose a job to Redis eviction, so the
    durable resolution sweep lives here.
    """
    from merlin_worker.schedules.cron import build_cron_scheduler, run_cron_forever

    assert callable(build_cron_scheduler)
    assert callable(run_cron_forever)


def test_sweep_unresolved_claims_signature() -> None:
    import inspect

    from merlin_worker.jobs.sweep_unresolved import sweep_unresolved_claims

    # RQ enqueues this by import path with no args — the CronScheduler
    # calls it bare. A signature drift here would break the cron enqueue.
    sig = inspect.signature(sweep_unresolved_claims)
    assert list(sig.parameters) == []


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


def test_config_resolution_sweep_defaults() -> None:
    """Q-030 sweep knobs have sane, on-by-default values."""
    from merlin_worker.config import Settings

    s = Settings()
    assert s.resolution_sweep_enabled is True
    assert s.resolution_sweep_interval_s == 900
    # Grace matches MVP §12.1's "more than 6 h in the past".
    assert s.resolution_sweep_grace_s == 21_600
    assert s.resolution_sweep_max_batch == 500
