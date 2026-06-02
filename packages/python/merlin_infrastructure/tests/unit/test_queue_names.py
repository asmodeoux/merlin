"""Queue-name constants — value pin + RQQueueAdapter behaviour.

Sister to ``tests/preflight/test_queue_wiring.py``; both pin the same
invariant from two angles. The preflight test verifies the producer
source files reach for the right constants; this file verifies the
constants themselves and the adapter's contract.
"""

from __future__ import annotations

import pytest
from merlin_infrastructure.queue.names import (
    ALL_QUEUE_NAMES,
    DEFAULT_QUEUE_NAME,
    REPUTATION_QUEUE_NAME,
    RESOLUTION_QUEUE_NAME,
)


def test_queue_name_constant_values() -> None:
    """The literal strings MUST match what RQ sees in Redis.

    Changing one of these values is a deploy event — every running
    worker has to be restarted with the new ``RQ_QUEUES`` env at the
    same time as the API/resolver ship. A unit test pin makes the
    change visible in code review.
    """
    assert DEFAULT_QUEUE_NAME == "default"
    assert RESOLUTION_QUEUE_NAME == "resolution"
    assert REPUTATION_QUEUE_NAME == "reputation"


def test_all_queue_names_contains_every_constant_once() -> None:
    """No duplicates; every constant appears in ``ALL_QUEUE_NAMES``."""
    assert set(ALL_QUEUE_NAMES) == {
        DEFAULT_QUEUE_NAME,
        RESOLUTION_QUEUE_NAME,
        REPUTATION_QUEUE_NAME,
    }
    assert len(ALL_QUEUE_NAMES) == len(set(ALL_QUEUE_NAMES))


def test_rq_queue_adapter_rejects_missing_queue_name() -> None:
    """``queue_name`` is a required kwarg — no silent default."""
    from merlin_infrastructure.queue.rq import RQQueueAdapter

    with pytest.raises(TypeError):
        RQQueueAdapter(redis=object())  # type: ignore[call-arg, arg-type]


def test_rq_queue_adapter_rejects_empty_queue_name() -> None:
    """Empty string ``queue_name`` MUST raise — RQ would happily accept
    it and park jobs on a queue with no worker."""
    from merlin_infrastructure.queue.rq import RQQueueAdapter

    with pytest.raises(ValueError, match="queue_name"):
        RQQueueAdapter(redis=object(), queue_name="")  # type: ignore[arg-type]


def test_rq_queue_adapter_exposes_queue_name_property() -> None:
    """Operators + preflight tests read ``adapter.queue_name`` to verify
    wiring without poking at private state."""
    import fakeredis
    from merlin_infrastructure.queue.rq import RQQueueAdapter

    adapter = RQQueueAdapter(
        redis=fakeredis.FakeStrictRedis(),
        queue_name=RESOLUTION_QUEUE_NAME,
    )
    assert adapter.queue_name == RESOLUTION_QUEUE_NAME
