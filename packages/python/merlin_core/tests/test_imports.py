"""Smoke test — merlin_core imports cleanly and ships its py.typed marker."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package and its three submodules import without error."""
    import merlin_core
    import merlin_core.application
    import merlin_core.application.ports
    import merlin_core.application.use_cases
    import merlin_core.contracts
    import merlin_core.domain

    assert merlin_core.__version__


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present so downstream packages can use our types."""
    assert importlib.resources.files("merlin_core").joinpath("py.typed").is_file()
