"""NotifierPort — outbound user notifications.

The worker calls this directly when it writes an outcome — it does NOT
relay through the bot service (one less hop, one less coupling). The
bot's role is purely interactive (handling incoming messages from
Telegram); outbound notifications come from whichever service has the
news.

Implemented by ``merlin_infrastructure.notify.telegram.TelegramNotifier``
(real Bot API via httpx) and ``merlin_infrastructure.notify.fake.FakeNotifier``
(tests — records calls).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotifierPort(Protocol):
    """Send a plain-text message to a Telegram user."""

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        """Send ``text`` to the Telegram user identified by ``tg_user_id``.

        All message rendering happens upstream — the adapter does not
        format, internationalize, or template. It takes a finished
        string and PUTs it on Telegram's Bot API. Templates live in
        ``merlin_infrastructure.notify.telegram.templates`` and follow
        the bot-voice rules (no version markers, no enum literals, no
        internal terminology).

        Errors are logged + Sentry-captured but do not raise. A failed
        notification must not prevent the outcome write from being
        committed — the user can always call ``/me`` to see results.
        """
        ...
