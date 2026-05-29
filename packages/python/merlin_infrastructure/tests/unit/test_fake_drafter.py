"""Unit tests for ``FakeClaimDrafter``."""

from __future__ import annotations

import pytest
from merlin_core.application.ports import ClaimDrafterError
from merlin_core.domain import ClaimDraft
from merlin_infrastructure.llm.fake import FakeClaimDrafter


async def test_registered_draft_returned_verbatim() -> None:
    drafter = FakeClaimDrafter()
    expected = ClaimDraft(direction="long", horizon="14d", conviction="strong")
    drafter.register("ETH long 14d strong", expected)

    actual = await drafter.draft("ETH long 14d strong")

    assert actual is expected
    assert drafter.calls == ["ETH long 14d strong"]


async def test_unregistered_text_raises_by_default() -> None:
    drafter = FakeClaimDrafter()
    with pytest.raises(ClaimDrafterError):
        await drafter.draft("never registered")


async def test_default_to_empty_yields_empty_draft_with_raw_turn() -> None:
    drafter = FakeClaimDrafter(default_to_empty=True)
    result = await drafter.draft("ETH long 14d strong")

    assert result.direction is None
    assert result.parse_metadata is not None
    assert result.parse_metadata.raw_user_turns == ("ETH long 14d strong",)
    assert result.parse_metadata.parsed_via == "freeform_llm"


async def test_registry_lookup_strips_input() -> None:
    drafter = FakeClaimDrafter()
    expected = ClaimDraft(direction="short")
    drafter.register("ETH dump", expected)

    assert await drafter.draft("  ETH dump  ") is expected
