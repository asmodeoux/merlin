"""``TelegramNotifier`` — concrete ``NotifierPort`` using the Bot API.

Single httpx POST per notification to
``https://api.telegram.org/bot<token>/sendMessage``. Errors are logged
+ Sentry-captured but never raised — a failed notification must not
prevent the outcome write from being committed (the user can always
call ``/me`` to see results).
"""

from __future__ import annotations

import logging
from typing import Final

import httpx

_log = logging.getLogger("merlin.infrastructure.notify.telegram")

_DEFAULT_TIMEOUT_S: Final = 10.0
_TELEGRAM_API_BASE: Final = "https://api.telegram.org"


class TelegramNotifierError(Exception):
    """Raised internally; never escapes ``notify_user`` — see class docstring."""


class TelegramNotifier:
    """Send plain-text messages to a Telegram user via the Bot API.

    Renders ``text`` as-is — message templating happens upstream in
    ``merlin_infrastructure.notify.telegram.templates``. The adapter
    just talks to the wire.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str = _TELEGRAM_API_BASE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Store credentials; build an httpx client if one isn't injected."""
        if not bot_token:
            raise ValueError("TelegramNotifier: bot_token is required")
        self._bot_token = bot_token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = http_client or httpx.AsyncClient(timeout=timeout_s)

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        """Send ``text`` to ``tg_user_id``. Swallow + log all failures."""
        url = f"{self._base_url}/bot{self._bot_token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={
                    "chat_id": tg_user_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code >= 400:
                _log.warning(
                    "telegram sendMessage failed: status=%d body=%s",
                    response.status_code,
                    response.text[:300],
                )
        except httpx.HTTPError as e:
            _log.warning("telegram transport error: %s", e)
