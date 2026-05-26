"""Smoke test — merlin_client imports cleanly + py.typed marker present."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package + public surface import without error."""
    import merlin_client
    from merlin_client import MerlinClient, MerlinClientError, MerlinHTTPError, MerlinTimeoutError

    assert merlin_client.__version__
    assert MerlinClient is not None
    assert issubclass(MerlinHTTPError, MerlinClientError)
    assert issubclass(MerlinTimeoutError, MerlinClientError)


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present."""
    assert importlib.resources.files("merlin_client").joinpath("py.typed").is_file()
