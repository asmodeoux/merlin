"""Make ``scripts/*.py`` importable from the tests without packaging.

The ``scripts/`` directory is intentionally not a Python package — it
holds standalone scripts run as ``uv run python scripts/foo.py``.  For
the tests we just want ``scripts/foo.py`` discoverable as a top-level
module, which a tiny ``sys.path`` shim handles cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
