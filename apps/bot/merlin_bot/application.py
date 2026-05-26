"""ApplicationBuilder wiring — central PTB v21 setup.

`build_application(settings)` returns a fully-wired `Application` ready
to run. Unit-testable: pass test Settings + assert the resulting app's
handlers / persistence / defaults.

`main.py` is a 5-line entry point that calls this + `run_polling()` (dev)
or `run_webhook()` (prod).
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder, Defaults

from merlin_bot.api_dependency import attach_client
from merlin_bot.commands_setup import register_commands
from merlin_bot.config import Settings
from merlin_bot.error_handler import handle as on_error
from merlin_bot.persistence import build_persistence

if TYPE_CHECKING:
    pass


async def _post_init(application: Application) -> None:
    """post_init — runs after the Application starts but before polling."""
    await register_commands(application)


def build_application(settings: Settings) -> Application:
    """Construct a fully-wired PTB v21 Application."""
    builder: ApplicationBuilder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .rate_limiter(AIORateLimiter())
        .defaults(
            Defaults(
                parse_mode=ParseMode.MARKDOWN_V2,
                tzinfo=dt.UTC,
                block=False,
            )
        )
        .persistence(build_persistence(settings))
        .post_init(_post_init)
    )

    application: Application = builder.build()

    # Sprint 1: register handlers
    #   application.add_handler(CommandHandler("start", handlers.start.on_start))
    #   application.add_handler(ConversationHandler(...))
    #   application.add_handler(CallbackQueryHandler(...))
    #   ...

    application.add_error_handler(on_error)

    # Stash a single MerlinClient on bot_data so handlers can pick it up
    # via context.bot_data["merlin_client"].
    attach_client(application, settings)

    return application
