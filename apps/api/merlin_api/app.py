"""FastAPI application factory.

`create_app()` builds and returns a fresh `FastAPI` instance — used both
by `merlin_api.main` (runtime) and `tests/conftest.py` (in-process via
`httpx.AsyncClient(app=create_app(), ...)`).

Layout:

- `/health`, `/ready`         — un-versioned, no auth (probes)
- `/v1/<resource>/...`        — business routes, HMAC bearer required
- `/openapi.json`, `/docs`, `/redoc` — schema + UIs
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from merlin_api import __version__, health
from merlin_api.auth import require_internal_token
from merlin_api.deps import get_settings
from merlin_api.errors import register_exception_handlers
from merlin_api.openapi import build_openapi


def create_app() -> FastAPI:
    """Build a FastAPI instance with health probes + the /v1 router mounted."""
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

    # /health, /ready — un-versioned, no auth (probes).
    app.include_router(health.router)

    # /v1/* — every business route lives here. The `dependencies=` on the
    # router applies the HMAC bearer to all child routes by default.
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_internal_token)])
    # Sprint 1: import and include resource routers here.
    #   v1.include_router(routes.claims.router)
    #   v1.include_router(routes.board.router)
    #   ...
    app.include_router(v1)

    register_exception_handlers(app)
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]

    return app


def get_app_info() -> dict[str, str]:
    """Small helper used by `main.py` / tests to assert the wiring is sane."""
    return {"name": "merlin-api", "version": __version__}


# Module-level `app` so `uvicorn merlin_api.app:app --reload` works as
# expected and matches the `--factory` flag on the prod entry point.
app = create_app()
