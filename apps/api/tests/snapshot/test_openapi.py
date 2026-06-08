"""OpenAPI snapshot test — fails if generated schema drifts from docs/openapi.json.

Marked `@pytest.mark.snapshot` so the pre-commit fast subset skips it
(syrupy can be expensive); CI runs it always and `make openapi-check`
covers the full diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OPENAPI_PATH = REPO_ROOT / "docs" / "openapi.json"


@pytest.mark.snapshot
def test_openapi_matches_committed_snapshot() -> None:
    """The schema we generate must match the committed docs/openapi.json byte-for-byte."""
    from apps.api.scripts.generate_openapi import render  # type: ignore[import-not-found]

    generated = render()
    assert OPENAPI_PATH.exists(), (
        "docs/openapi.json is missing — run `make openapi` and commit the snapshot."
    )
    committed = OPENAPI_PATH.read_text(encoding="utf-8")
    assert generated == committed, (
        "OpenAPI schema drift detected. Run `make openapi` and review the diff."
    )
