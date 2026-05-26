"""Generate (or check) the committed docs/openapi.json snapshot.

Usage:

    python apps/api/scripts/generate_openapi.py          # writes docs/openapi.json
    python apps/api/scripts/generate_openapi.py --check  # fails non-zero on drift

Wired into `make openapi` and `make openapi-check`, and into the CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from merlin_api.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "docs" / "openapi.json"


def render() -> str:
    """Build the OpenAPI document and serialize it deterministically."""
    schema = create_app().openapi()
    # Sort keys + 2-space indent so diffs are minimal and PR review is
    # actually readable.
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write() -> None:
    """Generate and write to disk."""
    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(render(), encoding="utf-8")
    print(f"  wrote {OPENAPI_PATH.relative_to(REPO_ROOT)}")


def check() -> int:
    """Compare generated output to the committed snapshot. Exit non-zero on drift."""
    generated = render()
    if not OPENAPI_PATH.exists():
        print(
            f"  {OPENAPI_PATH.relative_to(REPO_ROOT)} missing — run `make openapi` and commit.",
            file=sys.stderr,
        )
        return 1
    committed = OPENAPI_PATH.read_text(encoding="utf-8")
    if generated != committed:
        print(
            "OpenAPI schema drift — committed docs/openapi.json does not match the API.\n"
            "Run `make openapi`, review the diff, and commit the new snapshot.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    """CLI entry point — `--check` to verify the committed snapshot is fresh, else rewrite it."""
    parser = argparse.ArgumentParser(description="Generate or check docs/openapi.json.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail non-zero if generated output differs from the committed snapshot.",
    )
    args = parser.parse_args()
    if args.check:
        return check()
    write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
