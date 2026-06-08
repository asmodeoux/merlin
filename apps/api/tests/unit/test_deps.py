"""Regression tests for ``merlin_api.deps``.

These exist because the unit/integration suites override
``app.dependency_overrides[get_ports]`` and so never exercise the
real default factory. A request that flowed through the un-overridden
factory once crashed with ``TypeError: unhashable type: 'Settings'``
at FastAPI's dependency-solver stage — that bug shipped past CI and
only surfaced on live Docker traffic. These tests pin the contract
that the factory can be constructed and that ``get_settings()`` is
a hashable-arg-free singleton.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from merlin_api.deps import Ports, get_ports, get_settings


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_ports.cache_clear()


def test_get_settings_returns_singleton() -> None:
    """``get_settings`` is ``@lru_cache``'d — every call returns the
    same instance until the cache is cleared."""
    _clear_caches()
    with patch.dict(
        os.environ,
        {"MERLIN_API_INTERNAL_TOKEN": "x" * 16},
        clear=False,
    ):
        a = get_settings()
        b = get_settings()
    assert a is b


def test_get_ports_takes_no_arguments() -> None:
    """The factory must not declare a ``Depends(...)`` parameter.

    Pydantic v2 models (``Settings``) are mutable and therefore
    unhashable; combining ``@lru_cache`` with such a parameter
    crashes FastAPI's dependency solver at request time with
    ``TypeError: unhashable type: 'Settings'``. The factory should
    fetch settings internally via the cached ``get_settings()`` so
    that no Settings instance ever appears in the lru-cache key.
    """
    import inspect

    sig = inspect.signature(get_ports)
    assert list(sig.parameters) == [], (
        "get_ports must take no arguments — see deps.py NOTE for the regression history."
    )


def test_get_ports_constructs_without_hashability_crash() -> None:
    """Direct invocation must not raise — this is the path FastAPI
    takes after resolving the dependency graph."""
    _clear_caches()
    with patch.dict(
        os.environ,
        {
            "MERLIN_API_INTERNAL_TOKEN": "x" * 16,
            "OPENROUTER_API_KEY": "",
            "COINGECKO_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
            "REDIS_URL": "redis://localhost:6379/0",
            "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/test",
        },
        clear=False,
    ):
        ports = get_ports()
    assert isinstance(ports, Ports)
    assert ports.drafter is not None
    assert ports.market_data is not None
    assert ports.queue is not None
    assert ports.notifier is not None
