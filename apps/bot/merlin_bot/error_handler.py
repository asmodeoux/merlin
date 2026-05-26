"""Global error handler — Sentry forward + PII-stripped reply.

MVP §15 invariant 9: never log raw Telegram handles. The PTB Update
object carries `effective_user.username` and `effective_user.id`. Strip
both before forwarding to Sentry; the source_id is derived in the API
layer once the handler talks to MerlinClient.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


async def handle(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB error handler — forward to Sentry, reply with a generic message.

    Sprint 1:

    1. Strip PII (username, first_name, last_name, raw user_id) from the
       Update before serializing.
    2. `sentry_sdk.capture_exception(context.error, extras={"update": <stripped>})`.
    3. Reply: "Something went wrong on our side. This has been logged."
    """
