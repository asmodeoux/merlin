"""Telegram Bot API notifier — outbound user messages from the worker.

The worker writes outcomes, then calls ``notify_user(tg_user_id, text)``
directly. Bypassing the bot service eliminates a network hop and keeps
the bot's role purely interactive (handling incoming messages from
users).

The single concrete entry point is ``TelegramNotifier``. Notification
templates (``outcome_won``, ``outcome_lost``, ``outcome_flat``,
``rejected_asset_mutation``, ``resolution_failed_final``) live in
``templates.py`` and follow the bot-voice rules (plan §6):

- No version markers (``v0`` / ``MVP``).
- No internal model nouns (``hypothesis``, ``regret``, ``simulated``).
- Calendar dates only.
- The user's own words echoed verbatim where applicable.
"""

from merlin_infrastructure.notify.telegram.notifier import (
    TelegramNotifier,
    TelegramNotifierError,
)

__all__ = ["TelegramNotifier", "TelegramNotifierError"]
