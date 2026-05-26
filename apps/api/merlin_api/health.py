"""Liveness + readiness probes.

`/health` (liveness): the process is up and responding — no DB / Redis
required. Used by Fly for the simplest restart check.

`/ready` (readiness): the process can serve traffic — DB + Redis must
be reachable. Returns 503 when a dependency is down so Fly's load
balancer can drain traffic during a roll.

Both routes stay un-versioned (no `/v1` prefix) and skip the
`bearerAuth` requirement so probes don't need a token.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness response."""

    status: Literal["ok"] = "ok"
    version: str


class ReadyResponse(BaseModel):
    """Readiness response."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "fail"]]


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe — process is up.",
    description="Cheap check used by Fly's restart healthcheck. No DB or Redis required.",
    # Health probes don't need auth — Fly's prober shouldn't carry a secret.
    openapi_extra={"security": []},
)
async def health() -> HealthResponse:
    """Return 200 OK if the process is responding."""
    from merlin_api import __version__

    return HealthResponse(version=__version__)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe — process can serve traffic.",
    description=(
        "Returns 200 when DB + Redis are reachable, 503 otherwise so the "
        "load balancer can drain traffic. No auth — same rationale as /health."
    ),
    responses={
        status.HTTP_200_OK: {"model": ReadyResponse, "description": "Ready."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadyResponse,
            "description": "One or more dependencies unreachable.",
        },
    },
    openapi_extra={"security": []},
)
async def ready(response: Response) -> ReadyResponse:
    """Check critical dependencies; flip the status code on failure.

    Sprint 1 wires real connectivity probes against the SQLAlchemy
    session + Redis client. The scaffold returns "ready" so the rest of
    the stack (compose healthchecks, smoke tests) can come up.
    """
    # Sprint 1: replace the placeholder check with `select 1` against
    # the async DB session and a Redis `PING`.
    checks: dict[str, Annotated[str, "ok|fail"]] = {"db": "ok", "redis": "ok"}
    failed = [name for name, state in checks.items() if state != "ok"]
    if failed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready", checks=checks)  # type: ignore[arg-type]
    return ReadyResponse(status="ready", checks=checks)  # type: ignore[arg-type]
