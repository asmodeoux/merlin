"""OpenAPI metadata + security schemes.

Single `bearerAuth` scheme in v0 (HMAC bearer for service-to-service).
Phase 2 adds a second JWT scheme for the React frontend. We declare only
what we actually validate today — the OpenAPI document stays honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.openapi.utils import get_openapi

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Tags listed in the order they appear in the docs.
TAGS_METADATA: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness + readiness probes (un-versioned)."},
    {"name": "sources", "description": "Telegram-sourced contributors."},
    {"name": "claims", "description": "Alpha submissions."},
    {"name": "board", "description": "Leaderboards + windowed views."},
    {"name": "invites", "description": "Invite issuance + redemption."},
    {"name": "admin", "description": "Admin-only overrides + audit views."},
]

#: Single bearer-token security scheme — HMAC service-to-service in v0.
SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "HMAC bearer token for service-to-service requests "
            "(MERLIN_API_INTERNAL_TOKEN). Phase 2 adds a JWT scheme for "
            "the React frontend."
        ),
    },
}


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Build the OpenAPI 3.1 document with our metadata + security schemes.

    Wired in by `merlin_api.app.create_app` via
    `app.openapi = lambda: build_openapi(app)`. Deterministic — no clock,
    no env reads — so the snapshot test is stable.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.1.0",
        description=(
            "The canonical Merlin API. The Telegram bot is one client; "
            "the future React frontend and Pro Dashboard hit the same "
            "/v1 routes. See docs/ARCHITECTURE.md."
        ),
        routes=app.routes,
        tags=TAGS_METADATA,
        contact={"name": "Merlin Team"},
        license_info={"name": "Proprietary"},
    )

    # Components → securitySchemes — add ours; preserve anything FastAPI generated.
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.update(SECURITY_SCHEMES)

    # Default security applied to every /v1 route (health/ready stay open).
    # Per-route overrides win — handlers can declare `dependencies=[]` to
    # opt out for, e.g., a public Phase-2 landing-page endpoint.
    schema["security"] = [{"bearerAuth": []}]

    app.openapi_schema = schema
    return schema
