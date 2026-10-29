"""ClaimDrafterPort — turn freeform user text into a structured ``ClaimDraft``.

Implemented by ``merlin_infrastructure.llm.openrouter.OpenRouterClaimDrafter``
(real LLM) and ``merlin_infrastructure.llm.fake.FakeClaimDrafter`` (tests).

By design: a single call, ``temperature=0``, JSON-schema
constrained. The port doesn't know which model, which prompt, or which
provider — those are adapter concerns.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from merlin_core.domain import ClaimDraft


@runtime_checkable
class ClaimDrafterPort(Protocol):
    """Extract structured claim fields from a freeform user message."""

    async def draft(self, text: str) -> ClaimDraft:
        """Return a ``ClaimDraft`` for ``text``.

        Args:
            text: The user's raw freeform message. Whitespace-trimmed
                upstream; may be empty (the use case decides whether
                to call the drafter at all).

        Returns:
            A ``ClaimDraft`` with whatever fields the LLM could extract
            with confidence ≥ the adapter's per-field threshold. Fields
            below threshold appear in ``missing_fields`` rather than as
            low-confidence values.

        Raises:
            ClaimDrafterError: Any provider-side failure (5xx, rate
                limit, budget cap, malformed JSON). The use case catches
                this and falls back to the button form.
        """
        ...


class ClaimDrafterError(Exception):
    """Raised by any concrete ``ClaimDrafterPort`` adapter on failure.

    The use case catches this exception and falls back cleanly to the
    button-driven claim flow. Specific subclasses can be defined by
    adapters (timeout, budget-cap, malformed-output) but the use case
    treats them uniformly: drafter unavailable → use form.
    """
