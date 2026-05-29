"""Preflight checks — cross-cutting consistency tests.

These tests defend against the class of bug that escaped every existing
unit/contract suite during Sprint 1 Docker bring-up: defects that only
manifest when the stack is actually wired together (compose env_file
typos, service-name drift between scripts and the compose file, settings
that crash on real ``.env`` parsing, Dockerfiles that don't copy enough
to satisfy build-time metadata resolvers, FastAPI dependency factories
that crash under live traffic but never under ``dependency_overrides``).

Every test in this package should answer the question:

    "Was there a configuration mismatch between two files that would
    only ever bite us at runtime?"

Run via ``make ci`` — they live in the ``preflight`` marker and are
included in the default test path (``pyproject.toml`` ``testpaths``).
"""
