"""Deterministic ``ClaimDrafterPort`` adapter for tests.

Returns canned ``ClaimDraft``s keyed by input text. The test fixture
sets up the mapping; the adapter just looks them up. Tests that need
to assert "the adapter was called with X" can use a ``MagicMock``
backed by this class.

Usage::

    drafter = FakeClaimDrafter()
    drafter.register("ETH long 14d strong", ClaimDraft(direction="long", ...))
    use_case = SubmitClaim(drafter=drafter, ...)
"""

from __future__ import annotations

from datetime import UTC, datetime

from merlin_core.application.ports import ClaimDrafterError
from merlin_core.domain import ClaimDraft, ParseMetadata


class FakeClaimDrafter:
    """Maps input text → pre-registered ``ClaimDraft``.

    Unmapped inputs raise ``ClaimDrafterError`` by default (mirrors the
    "LLM failed" fallback path). Set ``default_to_empty=True`` to
    return an empty draft instead.
    """

    def __init__(self, *, default_to_empty: bool = False) -> None:
        """Initialise with an empty registry."""
        self._registry: dict[str, ClaimDraft] = {}
        self._default_to_empty = default_to_empty
        self.calls: list[str] = []

    def register(self, text: str, draft: ClaimDraft) -> None:
        """Map ``text`` to ``draft`` for future ``draft()`` calls."""
        self._registry[text.strip()] = draft

    async def draft(self, text: str) -> ClaimDraft:
        """Look up ``text`` in the registry; return registered draft or fallback."""
        self.calls.append(text)
        key = text.strip()
        if key in self._registry:
            return self._registry[key]
        if self._default_to_empty:
            return ClaimDraft(
                parse_metadata=ParseMetadata(
                    parsed_via="freeform_llm",
                    model="fake",
                    raw_user_turns=(text,),
                    extracted_at=datetime.now(UTC),
                )
            )
        raise ClaimDrafterError(f"FakeClaimDrafter: no registered draft for {key!r}")
