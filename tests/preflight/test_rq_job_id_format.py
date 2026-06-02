"""Preflight — every ``job_id`` format we use MUST pass RQ's validator.

Sprint-1 live regression: ``POST /claims`` (the Confirm-button path)
returned 500s because the API enqueued with ``job_id=f"resolve:{claim_id}"``
and RQ's ``validate_job_id`` rejects characters outside ``[A-Za-z0-9_-]``.
The unit tests all used ``FakeJobQueue`` which skipped that validation,
so the bug only surfaced under live traffic — exactly the kind of
cross-cutting wiring fault the preflight suite is for.

This test imports the real ``rq.utils.validate_job_id`` and asserts
that the two natural-key formats the codebase uses
(``resolve-<uuid>``, ``reputation-<uuid>``) round-trip cleanly. If
anyone reverts to colons (the historical, broken convention), CI
catches it before the next deploy.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from rq.utils import import_attribute  # noqa: F401  (sanity: rq importable)


def _validate(job_id: str) -> None:
    """Call RQ's validator, normalised across versions.

    RQ moved ``validate_job_id`` between ``rq.job`` and ``rq.utils``
    across releases. Try both so the test is pinned to the behaviour,
    not the import path.
    """
    try:
        from rq.utils import validate_job_id  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - older RQ layouts
        from rq.job import validate_job_id  # type: ignore[no-redef]
    validate_job_id(job_id)


def test_resolve_job_id_format_is_accepted_by_rq() -> None:
    """The ``POST /claims`` path enqueues ``resolve-{uuid}``. RQ must
    accept it — otherwise the user clicks Confirm and the API 500s."""
    job_id = f"resolve-{uuid4()}"
    _validate(job_id)


def test_reputation_job_id_format_is_accepted_by_rq() -> None:
    """The worker enqueues ``reputation-{uuid}`` as a follow-up after
    resolve_outcome. Same constraint applies."""
    job_id = f"reputation-{uuid4()}"
    _validate(job_id)


def test_reputation_job_id_helper_accepts_realistic_source_id() -> None:
    """The reputation enqueue uses ``source_id`` (``'tg:<32 hex>'``) in
    the job id, not a UUID. A previous version of this preflight only
    exercised the UUID shape and missed that the literal colon in
    ``tg:<hex>`` violates RQ's character set. Pin both the helper's
    contract and the real production format here so the next regression
    surfaces at CI, not on the Worker's retry-exhausted job log."""
    from merlin_core.domain.source_id import make_source_id
    from merlin_worker.jobs.resolve_outcome import _reputation_job_id

    source_id = make_source_id(42)
    job_id = _reputation_job_id(source_id)
    assert ":" not in job_id, f"reputation job_id still contains ':' — {job_id!r}"
    _validate(job_id)


def test_colon_separated_job_id_is_rejected_by_rq() -> None:
    """Sanity-pin the historical broken convention: a colon in the
    job id is exactly what RQ rejects. If RQ ever loosens this we'll
    learn about it here instead of in production."""
    with pytest.raises(ValueError, match="letters, numbers"):
        _validate(f"resolve:{uuid4()}")


def test_codebase_never_enqueues_with_colon_separator() -> None:
    """Grep guard — the codebase MUST NOT contain ``f"resolve:{`` or
    ``f"reputation:{`` job-id patterns anywhere outside this test
    file. Catches a revert before CI tests get a chance to."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for path in repo_root.rglob("*.py"):
        # Skip vendored deps and this file itself.
        if ".venv" in path.parts or path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for needle in ('f"resolve:{', 'f"reputation:{'):
            if needle in text:
                hits.append(f"{path.relative_to(repo_root)}: {needle}")
    assert not hits, (
        "Found colon-separated job-id format(s) — RQ rejects these. "
        "Use a dash: ``f'resolve-{...}'`` / ``f'reputation-{...}'``. "
        f"Offenders: {hits}"
    )
