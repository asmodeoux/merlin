"""Versioned prompt templates.

The prompt text is committed (not assembled at runtime from string
fragments) so the SHA-256 in ``claims.parse_metadata.prompt_hash`` is
deterministic and auditable. To bump the prompt:

1. Copy the current latest (e.g. ``claim_extraction_v2.md``) to the
   next version (``claim_extraction_v3.md``).
2. Edit the new version.
3. Update ``LATEST_CLAIM_EXTRACTION_PROMPT`` below to point at it.
4. Old claims still cite the previous version's hash in their
   ``parse_metadata`` — readable forever.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def load_prompt(name: str) -> str:
    """Read a prompt by filename (e.g. ``claim_extraction_v3.md``)."""
    resource = files("merlin_infrastructure.llm.prompts").joinpath(name)
    return Path(str(resource)).read_text(encoding="utf-8")


LATEST_CLAIM_EXTRACTION_PROMPT = "claim_extraction_v3.md"
