"""Smoke test — merlin_worker imports cleanly + py.typed marker present."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package + submodules import without error."""
    import merlin_worker
    import merlin_worker.config
    import merlin_worker.jobs
    import merlin_worker.schedules

    assert merlin_worker.__version__


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present."""
    assert importlib.resources.files("merlin_worker").joinpath("py.typed").is_file()
