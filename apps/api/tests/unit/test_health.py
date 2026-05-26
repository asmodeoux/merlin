"""Tests for /health + /ready — un-versioned, no auth."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.fast
async def test_health_returns_200(client: httpx.AsyncClient) -> None:
    """Liveness probe responds 200 with version metadata."""
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


@pytest.mark.fast
async def test_ready_returns_200(client: httpx.AsyncClient) -> None:
    """Readiness probe returns 200 when scaffold checks pass."""
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"db", "redis"}


@pytest.mark.fast
async def test_health_requires_no_auth(client: httpx.AsyncClient) -> None:
    """No Authorization header → still 200 (probes are public)."""
    response = await client.get("/health")
    assert response.status_code == 200
