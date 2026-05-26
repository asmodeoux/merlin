"""Shared FastAPI dependencies (cached settings, DB session, ...)."""

from __future__ import annotations

from functools import lru_cache

from merlin_api.config import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide singleton Settings.

    `BaseSettings` reads from the env on first call. Tests override this
    via `app.dependency_overrides[get_settings] = lambda: Settings(...)`.
    """
    return Settings()
