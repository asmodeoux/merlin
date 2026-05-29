"""Notification adapters — concrete implementations of ``NotifierPort``.

The worker calls these directly to send outcome notifications to users
(plan §3 — the bot service is not involved in outbound messaging at
v0; one less hop, one less coupling).

Sub-modules:

- ``telegram`` — production adapter using httpx against the Telegram
  Bot API. Notification templates live in ``telegram.templates`` and
  follow the bot-voice rules.
- ``fake`` — test adapter; records calls.
"""

from merlin_infrastructure.notify.fake import FakeNotifier
from merlin_infrastructure.notify.telegram import TelegramNotifier

__all__ = ["FakeNotifier", "TelegramNotifier"]
