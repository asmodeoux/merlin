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

from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    Defaults,
)

from merlin_bot.api_dependency import attach_client
from merlin_bot.commands_setup import register_commands
from merlin_bot.config import Settings
from merlin_bot.error_handler import handle as on_error
from merlin_bot.handlers.cancel import cancel_command
from merlin_bot.middleware.rate_limit import build_handler as build_rate_limit_handler
from merlin_bot.persistence import build_persistence

if TYPE_CHECKING:
    pass


def build_application(settings: Settings) -> Application:
    """Construct a fully-wired PTB v21 Application.

    The ordering inside ``_post_init`` is deliberate. PTB's lifecycle is::

        ApplicationBuilder().build()       # bot_data == {}
        application.bot_data = ...          # ← we set it here, but...
        application.run_polling()           # ...this calls:
            application.initialize()
                persistence._update_bot_data()  # ...which OVERWRITES bot_data
                                                #    with whatever persistence
                                                #    returns (an empty dict on
                                                #    a fresh boot with
                                                #    DictPersistence).
            post_init(application)              # runs AFTER persistence load
            polling.start()

    So setting ``bot_data`` before ``run_polling()`` is a no-op — every
    key we wrote gets clobbered. The fix is to write our keys inside
    ``post_init`` instead, which is the first point in the lifecycle
    where ``bot_data`` is the live, post-persistence dict. This is the
    PTB-blessed way to inject runtime singletons.
    """

    async def _post_init(application: Application) -> None:
        # Stash a single MerlinClient + admin allowlist on bot_data so
        # handlers can pick them up via ``context.bot_data[...]``. MUST
        # run after persistence load — see the build_application
        # docstring above.
        attach_client(application, settings)
        await register_commands(application, settings=settings)

    builder: ApplicationBuilder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .rate_limiter(AIORateLimiter())
        .defaults(
            # Plain-text by default. The bot voice rules (plan §6) keep
            # every user-facing string formatting-free; opting into
            # MarkdownV2 globally would force every `.`, `(`, `)`, `-`,
            # `!` to be escaped per the Telegram Bot API spec, and any
            # missed escape returns ``Bad Request: can't parse entities``
            # to the user. Handlers that genuinely need formatting can
            # pass ``parse_mode=ParseMode.MARKDOWN_V2`` per-message.
            Defaults(
                parse_mode=None,
                tzinfo=dt.UTC,
                block=False,
            )
        )
        .persistence(build_persistence(settings))
        .post_init(_post_init)
    )

    application: Application = builder.build()

    # Rate-limit TypeHandler runs at ``group=-1`` so PTB evaluates it
    # before any business handler in group 0. It throttles only the
    # ``/alpha`` LLM-trigger paths (OpenRouter cost protection); every
    # other handler — ``/board``, ``/me``, callback buttons, in-flow
    # text edits — passes through untouched. See
    # ``merlin_bot.middleware.rate_limit`` for the trigger heuristic
    # and the Redis-backed bucket logic.
    application.add_handler(build_rate_limit_handler(), group=-1)

    # Register handlers. Stateless ``CommandHandler``s come first so
    # they're reachable from inside any conversation — PTB matches
    # CommandHandlers ahead of state-matched MessageHandlers when the
    # message starts with ``/``. ``/help`` / ``/invite`` / ``/me`` /
    # ``/board`` / ``/tickers`` / ``/history`` are stateless. The
    # ``history:page:<offset>`` CallbackQueryHandler also lives in
    # this block — it's reachable from any non-conversation chat
    # state because the inline "Show 30 more" button can be tapped
    # after a long lull. Onboarding and ``/alpha`` (ConversationHandlers)
    # follow.
    from merlin_bot.handlers.admin import build_invite_handler, build_revoke_handler
    from merlin_bot.handlers.alpha import build_handler as build_alpha_handler
    from merlin_bot.handlers.board import (
        build_callback_handler as build_board_callback_handler,
    )
    from merlin_bot.handlers.board import build_handler as build_board_handler
    from merlin_bot.handlers.help import build_handler as build_help_handler
    from merlin_bot.handlers.history import (
        build_callback_handler as build_history_callback_handler,
    )
    from merlin_bot.handlers.history import build_handler as build_history_handler
    from merlin_bot.handlers.me import build_handler as build_me_handler
    from merlin_bot.handlers.start import build_handler as build_start_handler
    from merlin_bot.handlers.tickers import build_handler as build_tickers_handler

    application.add_handler(build_help_handler())
    application.add_handler(build_invite_handler())
    application.add_handler(build_revoke_handler())
    application.add_handler(build_me_handler())
    application.add_handler(build_board_handler())
    application.add_handler(build_board_callback_handler())
    application.add_handler(build_tickers_handler())
    application.add_handler(build_history_handler())
    application.add_handler(build_history_callback_handler())
    application.add_handler(build_start_handler())
    application.add_handler(build_alpha_handler())
    # Global ``/cancel`` — registered AFTER the ``/alpha`` ConversationHandler
    # so PTB only reaches it when the user isn't currently in an
    # ``/alpha`` state (in which case the conversation's own fallback
    # consumes the update). Without this, ``/cancel`` typed outside any
    # active conversation matches no handler and the bot stays silent
    # despite the command being advertised in the slash menu.
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_error_handler(on_error)

    return application
