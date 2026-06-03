"""Unit tests for ``FakeNotifier``."""

from __future__ import annotations

import pytest
from merlin_infrastructure.notify.fake import FakeNotifier


async def test_notify_appends_call() -> None:
    n = FakeNotifier()
    await n.notify_user(12345, "Your ETH call resolved.")

    assert len(n.calls) == 1
    assert n.calls[0].tg_user_id == 12345
    assert n.calls[0].text == "Your ETH call resolved."


async def test_notify_preserves_order_across_calls() -> None:
    n = FakeNotifier()
    await n.notify_user(1, "a")
    await n.notify_user(2, "b")
    await n.notify_user(3, "c")
    assert [c.text for c in n.calls] == ["a", "b", "c"]


async def test_raise_on_call_re_raises_after_recording() -> None:
    """The armed exception fires, AND the attempted call is recorded.

    Recording the call first means tests can assert both "the producer
    tried to notify" (the call landed in ``calls``) and "the consumer
    caught the failure" (the test reaches a post-raise assertion via
    ``pytest.raises``). Without the recording-before-raise ordering,
    tests for defensive try/except blocks couldn't distinguish "the
    notifier was never reached" from "the notifier was reached and
    raised cleanly".
    """
    boom = RuntimeError("simulated transport failure")
    n = FakeNotifier(raise_on_call=boom)

    with pytest.raises(RuntimeError, match="simulated transport failure"):
        await n.notify_user(42, "doesn't matter")

    assert len(n.calls) == 1
    assert n.calls[0].tg_user_id == 42
