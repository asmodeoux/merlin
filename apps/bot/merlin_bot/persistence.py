"""BasePersistence implementations — Redis for prod, Dict for dev.

NEVER use `PicklePersistence` in prod — it doesn't survive horizontal
scaling. Sprint 1 implements the Redis adapter; the scaffold ships the
class signature so other modules can `from .persistence import
build_persistence` without an import error.
"""

from __future__ import annotations

from telegram.ext import BasePersistence, DictPersistence

from merlin_bot.config import Settings


def build_persistence(settings: Settings) -> BasePersistence:
    """Pick the right BasePersistence impl for the current environment."""
    if settings.environment == "dev":
        # Dict persistence is in-memory; survives a single process run.
        return DictPersistence()
    # Sprint 1: replace with `RedisPersistence(settings.redis_url)`.
    return DictPersistence()
