"""Dockerfile ↔ workspace pyproject consistency.

Regression suite for bug #4 (Sprint 1 Docker bring-up): each workspace
``pyproject.toml`` declares ``readme = "README.md"`` (PEP 621 standard).
Hatchling validates that path at metadata-resolution time — i.e. during
the dependency-only ``uv sync --no-install-project`` step. The first
three Dockerfiles shipped without COPYing those READMEs into the image
at that stage, so every dev who ran ``make dev-up`` from a clean tree
got::

    OSError: Readme file does not exist: README.md
    help: `merlin-core` was included because `merlin-bot` (v0.1.0) depends
          on `merlin-core`

A static check on the Dockerfile contents is all we need — for every
workspace pyproject that says ``readme = "README.md"``, every Dockerfile
in the workspace that does ``uv sync --frozen`` must COPY the README
alongside the pyproject for the matching package.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILES = [
    _REPO_ROOT / "apps" / "api" / "Dockerfile",
    _REPO_ROOT / "apps" / "bot" / "Dockerfile",
    _REPO_ROOT / "apps" / "worker" / "Dockerfile",
]


def _packages_with_readme_requirement() -> list[Path]:
    """Every workspace pyproject.toml that declares ``readme = "README.md"``.

    These are the packages whose READMEs MUST appear at the path
    referenced before Hatchling tries to resolve their metadata.
    """
    candidates = [
        _REPO_ROOT / "apps" / "api" / "pyproject.toml",
        _REPO_ROOT / "apps" / "bot" / "pyproject.toml",
        _REPO_ROOT / "apps" / "worker" / "pyproject.toml",
        _REPO_ROOT / "packages" / "python" / "merlin_core" / "pyproject.toml",
        _REPO_ROOT / "packages" / "python" / "merlin_infrastructure" / "pyproject.toml",
        _REPO_ROOT / "packages" / "python" / "merlin_client" / "pyproject.toml",
    ]
    out: list[Path] = []
    for p in candidates:
        data = tomllib.loads(p.read_text())
        if data.get("project", {}).get("readme") == "README.md":
            out.append(p)
    return out


@pytest.fixture(scope="module")
def packages_needing_readme() -> list[Path]:
    return _packages_with_readme_requirement()


def test_every_referenced_readme_actually_exists(
    packages_needing_readme: list[Path],
) -> None:
    """Sanity-check: the pyprojects we'll demand are COPY'd must really
    have a README.md sitting next to them on disk."""
    for pyproject in packages_needing_readme:
        readme = pyproject.with_name("README.md")
        assert readme.exists(), (
            f"{pyproject.relative_to(_REPO_ROOT)} declares "
            f"readme='README.md' but {readme} doesn't exist on disk."
        )


@pytest.mark.parametrize("dockerfile", _DOCKERFILES, ids=lambda p: p.parent.name)
def test_dockerfile_copies_every_workspace_readme(
    dockerfile: Path,
    packages_needing_readme: list[Path],
) -> None:
    """Every Dockerfile that runs ``uv sync --frozen`` must COPY each
    workspace package's README.md into the build context before that
    sync — otherwise Hatchling fails at metadata-resolution time with
    a confusing chained error pointing at a transitive dependency.

    The fix shape is::

        COPY apps/bot/pyproject.toml apps/bot/README.md apps/bot/

    The test accepts any COPY whose target directory matches the
    package and whose source list contains both ``pyproject.toml`` and
    ``README.md``.
    """
    content = dockerfile.read_text()

    # Only assert against Dockerfiles that actually do ``uv sync``.
    if "uv sync" not in content:
        pytest.skip(f"{dockerfile.relative_to(_REPO_ROOT)} does not run uv sync")

    for pyproject in packages_needing_readme:
        pkg_dir = pyproject.parent.relative_to(_REPO_ROOT).as_posix()
        # Look for a single ``COPY`` line that mentions this package's
        # pyproject AND README. Both forms accepted:
        #   COPY <pkg>/pyproject.toml <pkg>/README.md <pkg>/
        #   COPY <pkg>/README.md <pkg>/pyproject.toml <pkg>/
        # plus the legacy 2-line form (1 line for each) as long as both exist.
        copies_readme = bool(
            re.search(
                rf"^\s*COPY\b[^\n]*\b{re.escape(pkg_dir)}/README\.md\b",
                content,
                re.MULTILINE,
            )
        )
        copies_pyproject = bool(
            re.search(
                rf"^\s*COPY\b[^\n]*\b{re.escape(pkg_dir)}/pyproject\.toml\b",
                content,
                re.MULTILINE,
            )
        )
        assert copies_pyproject, (
            f"{dockerfile.relative_to(_REPO_ROOT)} does not COPY "
            f"{pkg_dir}/pyproject.toml — Hatchling will fail to resolve "
            f"workspace metadata at ``uv sync`` time."
        )
        assert copies_readme, (
            f"{dockerfile.relative_to(_REPO_ROOT)} does not COPY "
            f"{pkg_dir}/README.md even though {pkg_dir}/pyproject.toml "
            f"declares readme='README.md'. The ``uv sync`` step will "
            f"fail with ``OSError: Readme file does not exist: "
            f"README.md`` chained through whichever package depends on "
            f"this one."
        )
