"""Observability — structlog + Sentry config.

Sprint 1 fills `setup_logging(settings)` (structlog JSON in prod, pretty
in dev) and `setup_sentry(settings)` (bare sentry-sdk; the FastAPI
integration lives in `merlin_api.app` so it doesn't leak into the bot /
worker images).
"""
