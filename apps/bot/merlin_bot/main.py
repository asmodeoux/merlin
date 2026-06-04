"""Entry point — boots PTB v21 in polling (dev) or webhook (prod) mode."""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings  # noqa: F401 — type re-export for tooling

from merlin_bot.application import build_application
from merlin_bot.config import Settings


def run() -> None:
    """Start the bot. Falls back to a clean exit when the token is empty."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    if not settings.telegram_bot_token:
        logging.warning("TELEGRAM_BOT_TOKEN is empty — bot not started.")
        return

    application = build_application(settings)

    if settings.telegram_mode == "webhook":
        if not settings.telegram_webhook_url:
            raise RuntimeError("telegram_mode=webhook but TELEGRAM_WEBHOOK_URL is empty")
        application.run_webhook(
            listen="0.0.0.0",  # noqa: S104 — container; Fly fronts it
            port=8080,
            webhook_url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret or None,
            # If the bot crashed and reconnects, drop whatever Telegram
            # queued in the meantime. Without this, a crash + reconnect
            # replays the entire backlog — which could include a wave
            # of /alpha messages from one user that would chew through
            # the OpenRouter budget all at once, bypassing the inbound
            # rate limiter that's keyed off live timestamps.
            drop_pending_updates=True,
        )
    else:
        application.run_polling(
            # Same rationale as the webhook branch above — a crash +
            # reconnect should not replay queued updates against
            # OpenRouter / CoinGecko.
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    run()
