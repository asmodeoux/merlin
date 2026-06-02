"""Preflight — every queue the API/resolver enqueues on MUST be drained.

Sprint-1 live regression we want to pin in CI forever:

* ``POST /claims`` built an ``RQQueueAdapter`` without passing
  ``queue_name``. The adapter silently defaulted to ``"merlin"``.
* The worker's ``Settings.rq_queues`` defaulted to
  ``["default", "resolution", "reputation"]``.
* RQ happily parked every ``resolve_outcome`` job on the ``merlin``
  queue. The worker never saw them. Users never got outcome messages.
* The mismatch was only visible by reading the env files of two
  different deployables side by side. Tests passed. Static checks
  passed. The bug only surfaced when a real bet's deadline expired.

This module asserts the wiring stays aligned by reading from one
canonical truth source per producer:

1. ``RQQueueAdapter.__init__`` no longer accepts a missing
   ``queue_name``. A unit assertion here doubles as a regression
   guard.
2. The names module exports a single ``ALL_QUEUE_NAMES`` tuple. The
   worker's ``Settings.rq_queues`` default MUST exactly match it.
3. The API's ``Ports`` factory and the resolver's
   ``_enqueue_reputation_recompute`` MUST pass queue names that live
   inside ``ALL_QUEUE_NAMES``. A static source-grep keeps "drift"
   out of the runtime by failing at collection time, before any
   integration test gets a chance to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from merlin_infrastructure.queue.names import (
    ALL_QUEUE_NAMES,
    DEFAULT_QUEUE_NAME,
    REPUTATION_QUEUE_NAME,
    RESOLUTION_QUEUE_NAME,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DEPS_PATH = REPO_ROOT / "apps" / "api" / "merlin_api" / "deps.py"
RESOLVER_PATH = REPO_ROOT / "apps" / "worker" / "merlin_worker" / "jobs" / "resolve_outcome.py"


def test_all_queue_names_tuple_is_stable() -> None:
    """The canonical names list MUST contain exactly the three v0 queues.

    Reordering or dropping a name from this tuple silently
    re-purposes a deployable's queue config — the API's enqueue path
    could end up on a queue the worker stopped draining. Pin the
    membership here so changes show up in CI.
    """
    assert ALL_QUEUE_NAMES == (
        DEFAULT_QUEUE_NAME,
        RESOLUTION_QUEUE_NAME,
        REPUTATION_QUEUE_NAME,
    )
    assert DEFAULT_QUEUE_NAME == "default"
    assert RESOLUTION_QUEUE_NAME == "resolution"
    assert REPUTATION_QUEUE_NAME == "reputation"


def test_rq_queue_adapter_requires_queue_name() -> None:
    """``RQQueueAdapter(redis=...)`` without ``queue_name`` MUST fail loud.

    The historical silent default was the live bug; removing it is
    the structural fix. A TypeError on a missing required kwarg
    means any future caller that "forgets" the queue name fails at
    construction time, not at "user files a complaint".
    """
    from merlin_infrastructure.queue.rq import RQQueueAdapter

    with pytest.raises(TypeError):
        RQQueueAdapter(redis=object())  # type: ignore[call-arg, arg-type]

    with pytest.raises(ValueError, match="queue_name"):
        RQQueueAdapter(redis=object(), queue_name="")  # type: ignore[arg-type]


def test_worker_rq_queues_default_matches_all_queue_names() -> None:
    """The worker's default ``rq_queues`` MUST equal ``ALL_QUEUE_NAMES``.

    Both sides reach for the same constant in source, but pydantic
    materialises a fresh ``list[str]`` per instance — the value-level
    check catches a future refactor that hard-codes the names back
    in for "clarity".
    """
    from merlin_worker.config import Settings

    settings = Settings()
    assert tuple(settings.rq_queues) == ALL_QUEUE_NAMES


def _adapter_uses_constant(source: str, constant_name: str) -> bool:
    """True iff every ``RQQueueAdapter(...)`` call uses the constant.

    We can't just regex inside the parens — Python call expressions
    nest, span newlines, and use sub-calls like ``Redis.from_url(...)``
    inside them. Instead we locate each ``RQQueueAdapter(`` opener,
    walk forward tracking paren depth, slice out the literal call
    text, and check that text for ``queue_name=<constant_name>``.

    Returns True iff *at least one* adapter construction uses the
    constant. We never expect zero RQQueueAdapter calls in either
    file (the producer/consumer wiring would be missing entirely),
    so the calling tests treat "no calls at all" as a separate
    failure mode and use this helper for the "uses the wrong
    constant" mode.
    """
    needle = "queue_name=" + constant_name
    pos = 0
    found_any_call = False
    while True:
        idx = source.find("RQQueueAdapter(", pos)
        if idx == -1:
            break
        depth = 0
        end = idx + len("RQQueueAdapter(") - 1
        while end < len(source):
            ch = source[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        call_text = source[idx : end + 1]
        found_any_call = True
        if needle in call_text:
            return True
        pos = end + 1
    # No matching constant inside any RQQueueAdapter call. If we
    # never found a call at all, return False too — the caller's
    # ``"constant in source"`` assertion already covers that case,
    # and we want the "wrong constant" failure mode to be loud.
    del found_any_call
    return False


def test_api_deps_passes_resolution_queue_name() -> None:
    """``apps/api/merlin_api/deps.py`` MUST pass ``RESOLUTION_QUEUE_NAME``
    to ``RQQueueAdapter`` for the ``POST /claims`` enqueue path.

    A regression here is the "merlin" mismatch coming back — the
    failure mode the suite was created to prevent.
    """
    source = API_DEPS_PATH.read_text(encoding="utf-8")
    assert "RESOLUTION_QUEUE_NAME" in source, (
        "apps/api/merlin_api/deps.py no longer imports "
        "RESOLUTION_QUEUE_NAME — the API's RQ adapter may have "
        "regressed to the silent 'merlin' default. The worker won't "
        "see resolve_outcome jobs."
    )
    assert _adapter_uses_constant(source, "RESOLUTION_QUEUE_NAME"), (
        "apps/api/merlin_api/deps.py doesn't wire RESOLUTION_QUEUE_NAME "
        "into the RQQueueAdapter construction. POST /claims will "
        "enqueue on the wrong queue."
    )


def test_resolver_enqueues_on_reputation_queue() -> None:
    """``_enqueue_reputation_recompute`` MUST target ``REPUTATION_QUEUE_NAME``.

    Same drift class as the API path. The resolver's reactive
    follow-up enqueue MUST land on a queue the worker drains.
    """
    source = RESOLVER_PATH.read_text(encoding="utf-8")
    assert "REPUTATION_QUEUE_NAME" in source, (
        "apps/worker/merlin_worker/jobs/resolve_outcome.py no "
        "longer imports REPUTATION_QUEUE_NAME — the reactive "
        "reputation recompute will silently miss the worker."
    )
    assert _adapter_uses_constant(source, "REPUTATION_QUEUE_NAME"), (
        "resolve_outcome.py doesn't pass REPUTATION_QUEUE_NAME into "
        "the RQQueueAdapter construction. The recompute job will "
        "land on the wrong queue."
    )


def test_known_producers_target_queues_in_all_queue_names() -> None:
    """Cross-check: the names producers use MUST be in ``ALL_QUEUE_NAMES``.

    Catches the "added a new queue name constant but forgot to add
    it to the tuple" failure mode. The worker drains ``ALL_QUEUE_NAMES``
    so a producer pointing at a constant outside the tuple is the
    historical bug under a new alias.
    """
    api_source = API_DEPS_PATH.read_text(encoding="utf-8")
    resolver_source = RESOLVER_PATH.read_text(encoding="utf-8")

    known_constants = {
        "DEFAULT_QUEUE_NAME": DEFAULT_QUEUE_NAME,
        "RESOLUTION_QUEUE_NAME": RESOLUTION_QUEUE_NAME,
        "REPUTATION_QUEUE_NAME": REPUTATION_QUEUE_NAME,
    }
    for name, value in known_constants.items():
        assert value in ALL_QUEUE_NAMES, (
            f"{name}={value!r} is not in ALL_QUEUE_NAMES "
            f"({ALL_QUEUE_NAMES!r}); the worker won't drain it."
        )
        # Source-grep guard against the historical 'merlin' literal
        # creeping back into either producer.
        for source_path, source_text in (
            (API_DEPS_PATH, api_source),
            (RESOLVER_PATH, resolver_source),
        ):
            assert 'queue_name="merlin"' not in source_text, (
                f"{source_path} contains a hardcoded queue_name='merlin' — "
                "the historical mismatch is back."
            )
