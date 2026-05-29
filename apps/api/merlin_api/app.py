"""FastAPI application factory.

`create_app()` builds and returns a fresh `FastAPI` instance — used both
by `merlin_api.main` (runtime) and `tests/conftest.py` (in-process via
`httpx.AsyncClient(app=create_app(), ...)`).

Layout:

- `/health`, `/ready`           — no auth (probes)
- `/invites`, `/me`, `/board`,
  `/assets`, `/claims`          — flat business routes, HMAC bearer required
- `/openapi.json`, `/docs`,
  `/redoc`                      — schema + UIs

No URL versioning at v0 — see the bot-flow plan §6 and ADR (s) in
``docs/ARCHITECTURE.md``. The OpenAPI ``info.version`` field
(``Settings.api_version``) is the canonical version marker; bumped
manually when the contract changes; CI's snapshot diff catches drift.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from merlin_api import __version__, health
from merlin_api.auth import require_internal_token
from merlin_api.deps import get_settings
from merlin_api.errors import register_exception_handlers
from merlin_api.openapi import build_openapi
from merlin_api.routes import assets, board, claims, invites, me, sources


def create_app() -> FastAPI:
    """Build a FastAPI instance with health probes + flat business routes."""
    settings = get_settings()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        # We override `app.openapi` below with our own builder that adds
        # the security schemes + default security.
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # /health, /ready — no auth (probes).
    app.include_router(health.router)

    # Business routes — flat, HMAC bearer required by default. Each
    # router module declares its own paths; the dependency below
    # applies to every business route at once.
    auth_dep = [Depends(require_internal_token)]
    app.include_router(invites.router, dependencies=auth_dep)
    app.include_router(sources.router, dependencies=auth_dep)
    app.include_router(me.router, dependencies=auth_dep)
    app.include_router(board.router, dependencies=auth_dep)
    app.include_router(assets.router, dependencies=auth_dep)
    app.include_router(claims.router, dependencies=auth_dep)

    register_exception_handlers(app)
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]

    return app


def get_app_info() -> dict[str, str]:
    """Small helper used by `main.py` / tests to assert the wiring is sane."""
    return {"name": "merlin-api", "version": __version__}


# Module-level `app` so `uvicorn merlin_api.app:app --reload` works as
# expected and matches the `--factory` flag on the prod entry point.
app = create_app()
