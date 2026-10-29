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

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from merlin_api.config import Settings
from merlin_api.deps import get_settings

router = APIRouter(tags=["health"])

_log = logging.getLogger("merlin.api.health")


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
async def ready(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> ReadyResponse:
    """Probe DB + Redis; flip to 503 on failure so Fly drains traffic.

    No external API checks here — OpenRouter / CoinGecko / Telegram outages don't
    make us "not ready"; the LLM circuit-breaker and resolver retries
    handle them at request time.
    """
    checks: dict[str, Literal["ok", "fail"]] = {
        "db": await _check_db(settings.database_url),
        "redis": await _check_redis(settings.redis_url),
    }
    if any(state != "ok" for state in checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready", checks=checks)
    return ReadyResponse(status="ready", checks=checks)


async def _check_db(database_url: str) -> Literal["ok", "fail"]:
    """Run ``SELECT 1`` against the configured Postgres DSN.

    Engine is created per-call. We do not cache a session here because
    readiness must catch DSN/credential drift between rolling restarts.
    """
    try:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(database_url, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
    except Exception as e:
        _log.warning("readiness db check failed: %s", e)
        return "fail"
    return "ok"


async def _check_redis(redis_url: str) -> Literal["ok", "fail"]:
    """``PING`` the configured Redis instance via the asyncio client."""
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()
    except Exception as e:
        _log.warning("readiness redis check failed: %s", e)
        return "fail"
    return "ok"


# Re-export Annotated to keep mypy happy in older Python type-check matrices.
_ = Annotated
