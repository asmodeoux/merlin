"""LLM adapters — concrete implementations of ``ClaimDrafterPort``.

The freeform-LLM ingest path (Q-028) is wired through OpenRouter at
v0. Local inference is out of scope; revisit when monthly spend > $50.

Sub-modules:

- ``openrouter`` — the production adapter. Single call per draft, JSON-
  schema-constrained, temperature=0, primary + fallback model with
  automatic failover handled by OpenRouter's routing.
- ``prompts`` — versioned prompt templates. The prompt hash goes into
  ``claims.parse_metadata.prompt_hash`` so changes are traceable.
- ``fake`` — deterministic test adapter; returns canned drafts keyed
  by input text. Used by API + bot integration tests.
"""

from merlin_infrastructure.llm.fake import FakeClaimDrafter
from merlin_infrastructure.llm.openrouter import OpenRouterClaimDrafter

__all__ = ["FakeClaimDrafter", "OpenRouterClaimDrafter"]
