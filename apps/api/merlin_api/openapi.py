"""OpenAPI metadata + security schemes.

Single `bearerAuth` scheme (HMAC bearer for service-to-service).
Phase 2 adds a second JWT scheme for a React frontend. We declare only
what we actually validate today — the OpenAPI document stays honest.

Versioning policy: no URL prefix at v0. The OpenAPI ``info.version``
field is the canonical API version marker — bumped manually when the
contract changes; CI's snapshot diff catches drift. See
ADR (s) in ``docs/ARCHITECTURE.md``.
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
            "future surfaces (React frontend, Pro Dashboard) hit the same "
            "flat routes. See docs/ARCHITECTURE.md."
        ),
        routes=app.routes,
        tags=TAGS_METADATA,
        contact={"name": "asmodeoux", "url": "https://github.com/asmodeoux/merlin"},
        license_info={"name": "MIT"},
    )

    # Components → securitySchemes — add ours; preserve anything FastAPI generated.
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes.update(SECURITY_SCHEMES)

    # Default security applied to every business route. Health and
    # readiness probes opt out via ``openapi_extra={"security": []}``
    # on their route decorators (see merlin_api.health).
    schema["security"] = [{"bearerAuth": []}]

    app.openapi_schema = schema
    return schema
