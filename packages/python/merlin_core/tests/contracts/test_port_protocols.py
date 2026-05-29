"""Port contract tests — every concrete fake honours the Protocol shape.

These guard the hexagonal boundary: the test suite never imports an
adapter's concrete class to satisfy a port-typed parameter; it depends
on the Protocol's ``runtime_checkable`` instance check.
"""

from __future__ import annotations

from merlin_core.application.ports import (
    ClaimDrafterPort,
    JobQueuePort,
    MarketDataPort,
    NotifierPort,
)


def test_fake_claim_drafter_satisfies_protocol() -> None:
    from merlin_infrastructure.llm.fake import FakeClaimDrafter

    assert isinstance(FakeClaimDrafter(), ClaimDrafterPort)


def test_fake_market_data_satisfies_protocol() -> None:
    from merlin_infrastructure.market_data.fake import FakeMarketData

    assert isinstance(FakeMarketData(), MarketDataPort)


def test_fake_job_queue_satisfies_protocol() -> None:
    from merlin_infrastructure.queue.fake import FakeJobQueue

    assert isinstance(FakeJobQueue(), JobQueuePort)


def test_fake_notifier_satisfies_protocol() -> None:
    from merlin_infrastructure.notify.fake import FakeNotifier

    assert isinstance(FakeNotifier(), NotifierPort)
