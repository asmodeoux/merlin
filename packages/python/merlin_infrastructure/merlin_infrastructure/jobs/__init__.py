"""RQ helpers — queue + worker plumbing.

This module ships helpers (queue setup, retry policy, telemetry hooks);
the actual job functions live in `merlin_worker.jobs/` so the worker
service owns its workload definition.
"""
