"""Compose ↔ helper-script consistency.

Regression suite for two real Sprint-1 defects that shipped past CI:

* ``deploy/docker-compose.dev.yml`` had ``env_file: ../apps/api/.env.example``
  for every service. Real secrets live in ``.env``; ``.env.example`` is the
  placeholder. Booting silently produced a bot with an empty token and an
  API with the LLM disabled. CI never caught it because no test parsed
  the compose file as truth.

* ``scripts/dev_verify.sh`` referenced ``bot`` / ``worker`` / ``api`` as
  compose service names, but the compose file actually declares
  ``merlin-bot`` / ``merlin-worker`` / ``merlin-api``. ``make dev-verify``
  would always fail at the log-grep stage even with a healthy stack.
  CI never caught it because nothing cross-validated the two files.

Both classes of defect are eliminated by parsing the compose YAML as the
single source of truth and asserting every consumer agrees with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_DEV = _REPO_ROOT / "deploy" / "docker-compose.dev.yml"
_DEV_VERIFY = _REPO_ROOT / "scripts" / "dev_verify.sh"


def _load_compose() -> dict:
    return yaml.safe_load(_COMPOSE_DEV.read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parsed ``docker-compose.dev.yml``."""
    return _load_compose()


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose["services"]


# ---------------------------------------------------------------------------
# Regression for bug #1 — env_file must point at the real .env, not the
# placeholder .env.example.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["merlin-api", "merlin-bot", "merlin-worker"])
def test_app_service_env_file_points_at_real_env(services: dict, service: str) -> None:
    """Every Python app service must read its real ``.env``.

    Booting with ``.env.example`` (empty placeholders) silently
    disables the LLM, leaves the bot tokenless, etc. — none of which
    surfaces as a startup error, so it would have to bite us in
    production to be noticed otherwise.
    """
    env_file = services[service].get("env_file")
    assert env_file is not None, f"{service}: env_file missing"
    assert isinstance(env_file, str), f"{service}: env_file must be a single path"
    assert env_file.endswith("/.env"), (
        f"{service}: env_file is {env_file!r} — must end with '/.env', "
        f"never '/.env.example' or any other placeholder."
    )


# ---------------------------------------------------------------------------
# Regression for bug #2 — dev_verify.sh must reference service names that
# actually exist in the compose file.
# ---------------------------------------------------------------------------


def _services_named_in_dev_verify() -> set[str]:
    """Pull every service name dev_verify.sh refers to via compose subcommands."""
    text = _DEV_VERIFY.read_text()
    # Patterns we care about:
    #   docker compose ... logs <service>
    #   wait_for <service> healthy
    #   dump_logs <service>
    patterns = [
        r"docker compose [^\n]*logs (?:--tail=\d+ )?([a-z][a-z0-9_-]+)",
        r"\bwait_for ([a-z][a-z0-9_-]+) ",
        r"\bdump_logs ([a-z][a-z0-9_-]+)",
    ]
    found: set[str] = set()
    for pat in patterns:
        found.update(re.findall(pat, text))
    # Strip shell variables that look like names but aren't (e.g. ``$service``).
    return {s for s in found if not s.startswith("$")}


def test_dev_verify_only_references_real_services(compose: dict) -> None:
    """Every service name dev_verify.sh references must exist in the compose.

    Bug #2 was the script using ``bot`` / ``worker`` / ``api`` against a
    compose that declared ``merlin-bot`` / ``merlin-worker`` /
    ``merlin-api``. dev-verify always failed at the log-grep stage even
    on a healthy stack.
    """
    real = set(compose["services"].keys())
    referenced = _services_named_in_dev_verify()
    unknown = referenced - real
    assert not unknown, (
        f"dev_verify.sh references compose services that don't exist: "
        f"{sorted(unknown)}. Real services: {sorted(real)}."
    )


def test_dev_verify_references_every_app_service(compose: dict) -> None:
    """The script must log-grep every Python app service.

    If we add a new app to compose, dev-verify should learn about it.
    """
    referenced = _services_named_in_dev_verify()
    for app in ("merlin-api", "merlin-bot", "merlin-worker"):
        assert app in referenced, (
            f"dev_verify.sh never references {app!r}; it should at least "
            f"dump its logs on failure. Add a ``dump_logs {app}`` or "
            f"wait/grep step."
        )


# ---------------------------------------------------------------------------
# Compose hygiene — defends against the broader class of "the compose
# file drifts and we boot something subtly wrong".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["merlin-api", "merlin-bot", "merlin-worker"])
def test_app_service_mounts_packages_for_hot_reload(services: dict, service: str) -> None:
    """Dev compose must bind-mount ``packages/python`` so cross-package
    edits trigger uvicorn / watchmedo reloads. A regression here would
    silently turn every save into "you need a rebuild" — pure friction."""
    volumes = services[service].get("volumes") or []
    has_packages_mount = any(v.split(":")[0].endswith("/packages/python") for v in volumes)
    assert has_packages_mount, (
        f"{service} doesn't bind-mount ../packages/python — hot-reload "
        f"will miss cross-package edits."
    )


def test_compose_postgres_remap_to_avoid_native_clash(services: dict) -> None:
    """Postgres host port must NOT be 5432 — too many devs have a native
    PG running. The remap to 5433 is intentional; if anyone reverts it,
    the next dev with a native PG will hit a confusing 'port in use'."""
    ports = services["postgres"].get("ports") or []
    assert any(p.startswith("5433:") for p in ports), (
        f"postgres ports {ports!r} — host port should be 5433 (container "
        f"keeps 5432). Reverting risks a clash with a native PG. See "
        f"docker-compose.dev.yml comment for the rationale."
    )


def test_compose_redis_remap_to_avoid_native_clash(services: dict) -> None:
    """Same as above for Redis (host 6380 → container 6379)."""
    ports = services["redis"].get("ports") or []
    assert any(p.startswith("6380:") for p in ports), (
        f"redis ports {ports!r} — host port should be 6380 (container "
        f"keeps 6379). Reverting risks a clash with a native Redis."
    )
