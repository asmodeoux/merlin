"""Unit tests for ``FakeNotifier``."""

from __future__ import annotations

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
