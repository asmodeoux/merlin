"""Smoke test — merlin_api imports cleanly + py.typed marker present."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package + submodules import without error."""
    import merlin_api
    import merlin_api.app
    import merlin_api.auth
    import merlin_api.config
    import merlin_api.deps
    import merlin_api.errors
    import merlin_api.health
    import merlin_api.openapi
    import merlin_api.routes

    assert merlin_api.__version__


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present."""
    assert importlib.resources.files("merlin_api").joinpath("py.typed").is_file()
