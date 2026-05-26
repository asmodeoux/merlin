"""Inject a single MerlinClient into application.bot_data at startup.

Handlers retrieve it via `context.bot_data["merlin_client"]`. No
globals. The same httpx connection pool serves the whole process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from merlin_client import MerlinClient

if TYPE_CHECKING:
    from telegram.ext import Application

    from merlin_bot.config import Settings


BOT_DATA_KEY = "merlin_client"


def attach_client(application: Application, settings: Settings) -> None:
    """Construct + stash the MerlinClient on application.bot_data."""
    client = MerlinClient(
        base_url=settings.merlin_api_base_url,
        token=settings.merlin_api_internal_token,
        timeout=settings.merlin_api_timeout_s,
    )
    application.bot_data[BOT_DATA_KEY] = client
