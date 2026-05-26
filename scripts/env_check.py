"""env-check — assert every apps/*/.env.example matches its Settings.

Run via `make env-check`. Fails the build (and the CI pipeline) when:

- a Settings field has no matching key in `.env.example`
- a `.env.example` key has no matching Settings field

Adding / renaming an env var requires updating both in the same commit.

This script lives at the repo root (not inside an app) on purpose — it
walks every `apps/*/` so we don't have to maintain a per-app script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = REPO_ROOT / "apps"
ENV_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)

# (slug under apps/, dotted Settings module relative to merlin_<slug>/).
APPS: tuple[tuple[str, str], ...] = (
    ("api", "merlin_api.config"),
    ("bot", "merlin_bot.config"),
    ("worker", "merlin_worker.config"),
)


def _read_env_example_keys(path: Path) -> set[str]:
    """Extract every uppercase KEY= entry from a .env file."""
    text = path.read_text(encoding="utf-8")
    return {match.group(1) for match in ENV_KEY_RE.finditer(text)}


def _read_settings_fields(module_path: str) -> set[str]:
    """Import the Settings class from `module_path` and return its uppercase field names."""
    import importlib

    module = importlib.import_module(module_path)
    settings_cls = module.Settings
    # pydantic-settings normalizes keys to upper-case at env-read time;
    # the canonical KEY is the field name in upper case.
    return {name.upper() for name in settings_cls.model_fields}


def _check(slug: str, settings_module: str) -> list[str]:
    """Return a list of error messages for one app (empty list means OK)."""
    errors: list[str] = []
    env_example = APPS_DIR / slug / ".env.example"
    if not env_example.exists():
        return [f"  apps/{slug}/.env.example is missing"]

    try:
        settings_keys = _read_settings_fields(settings_module)
    except ImportError as exc:  # pragma: no cover — failure mode for un-installed envs
        return [f"  cannot import {settings_module}: {exc}"]

    env_keys = _read_env_example_keys(env_example)

    missing_in_env = settings_keys - env_keys
    missing_in_settings = env_keys - settings_keys

    for key in sorted(missing_in_env):
        errors.append(
            f"  apps/{slug}: Settings field `{key}` has no matching key in apps/{slug}/.env.example"
        )
    for key in sorted(missing_in_settings):
        errors.append(
            f"  apps/{slug}: apps/{slug}/.env.example key `{key}` has no "
            f"matching field in {settings_module}.Settings"
        )
    return errors


def main() -> int:
    """CLI entry point — exit non-zero if any app's Settings and .env.example diverge."""
    failures: list[str] = []
    for slug, settings_module in APPS:
        failures.extend(_check(slug, settings_module))

    if failures:
        print("env-check failed:", file=sys.stderr)
        for msg in failures:
            print(msg, file=sys.stderr)
        print(
            "\n  Fix: add / rename the missing key in BOTH the Settings "
            "class AND the .env.example in the same commit.",
            file=sys.stderr,
        )
        return 1

    print(f"  env-check ok ({len(APPS)} apps checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
