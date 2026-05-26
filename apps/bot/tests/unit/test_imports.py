"""Smoke test — merlin_bot imports cleanly + py.typed marker present."""

from __future__ import annotations

import importlib.resources

import pytest


@pytest.mark.fast
def test_package_imports() -> None:
    """Top-level package + submodules import without error."""
    import merlin_bot
    import merlin_bot.api_dependency
    import merlin_bot.application
    import merlin_bot.commands_setup
    import merlin_bot.config
    import merlin_bot.error_handler
    import merlin_bot.filters
    import merlin_bot.formatting
    import merlin_bot.persistence

    assert merlin_bot.__version__


@pytest.mark.fast
def test_py_typed_marker_shipped() -> None:
    """PEP 561 marker is present."""
    assert importlib.resources.files("merlin_bot").joinpath("py.typed").is_file()
