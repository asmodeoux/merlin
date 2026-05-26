"""Merlin application layer — use cases + ports.

Use cases orchestrate domain logic via Protocols defined in
`merlin_core.application.ports`. Concrete adapters (SQLAlchemy,
CoinGecko, Sentry) live in `merlin_infrastructure` and implement those
ports. The application layer never imports SQLAlchemy, FastAPI, Telegram,
httpx, or any infrastructure module (enforced by import-linter).
"""
