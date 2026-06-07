"""Fly.io ``release_command`` consistency.

Regression suite for the migration-race class of defect: alembic
runs as a Fly ``release_command`` only on the API app (the singular
HTTP-serving service). If a future PR accidentally copies the
``[deploy] release_command`` block onto ``apps/bot/fly.toml`` or
``apps/worker/fly.toml``, two or three release processes would race
on ``alembic_version`` and at best one would crash with a duplicate-row
error, at worst they would deadlock mid-migration.

The fix is structural: pin the invariant in a preflight test so the
mistake fails ``make ci`` instead of fly-up. Mirrors the pattern in
:mod:`tests.preflight.test_compose_consistency`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_API_FLY = _REPO_ROOT / "apps" / "api" / "fly.toml"
_BOT_FLY = _REPO_ROOT / "apps" / "bot" / "fly.toml"
_WORKER_FLY = _REPO_ROOT / "apps" / "worker" / "fly.toml"


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def test_release_command_is_set_on_api() -> None:
    """``apps/api/fly.toml`` must declare an alembic upgrade release command."""
    cfg = _load(_API_FLY)
    deploy = cfg.get("deploy")
    assert deploy is not None, (
        f"{_API_FLY.relative_to(_REPO_ROOT)} is missing the [deploy] table. "
        'Add `release_command = "uv run --package merlin-api alembic ... upgrade head"` '
        "so every deploy runs migrations before traffic flips."
    )
    cmd = deploy.get("release_command", "")
    assert "alembic" in cmd and "upgrade head" in cmd, (
        f"{_API_FLY.relative_to(_REPO_ROOT)} [deploy] release_command must "
        f"invoke `alembic upgrade head`, got: {cmd!r}"
    )


@pytest.mark.parametrize(
    "fly_toml",
    [
        pytest.param(_BOT_FLY, id="bot"),
        pytest.param(_WORKER_FLY, id="worker"),
    ],
)
def test_release_command_is_not_set_on_non_api_apps(fly_toml: Path) -> None:
    """Bot/worker fly.toml must NOT carry release_command — that would race.

    Only one app may own the schema-migration step (the API). If you need
    a per-app one-shot at release time, add a dedicated process group
    instead of a release_command — and document the non-racing
    interaction with the API's release_command here first.
    """
    cfg = _load(fly_toml)
    deploy = cfg.get("deploy", {})
    assert "release_command" not in deploy, (
        f"{fly_toml.relative_to(_REPO_ROOT)} declares a [deploy] "
        "release_command. Only apps/api/fly.toml may run migrations on "
        "release; duplicating the alembic upgrade across apps races on "
        "alembic_version. Remove the release_command here or, if you "
        "genuinely need a per-app one-shot, add an issue + a new "
        "preflight assertion proving the two commands don't race."
    )
