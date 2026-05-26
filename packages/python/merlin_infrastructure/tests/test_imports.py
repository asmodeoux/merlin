"""Smoke test — merlin_infrastructure imports cleanly + py.typed marker present."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package and submodules import without error."""
    import merlin_infrastructure
    import merlin_infrastructure.db
    import merlin_infrastructure.db.models
    import merlin_infrastructure.db.session
    import merlin_infrastructure.jobs
    import merlin_infrastructure.market_data
    import merlin_infrastructure.observability

    assert merlin_infrastructure.__version__


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present."""
    assert importlib.resources.files("merlin_infrastructure").joinpath("py.typed").is_file()
