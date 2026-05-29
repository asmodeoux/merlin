"""Tests for /health + /ready — un-versioned, no auth."""

from __future__ import annotations

import httpx
import pytest
from merlin_api import health as health_module


@pytest.fixture(autouse=True)
def _stub_readiness_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the DB + Redis probes so unit tests don't need live services."""

    async def _ok_db(_url: str) -> str:
        return "ok"

    async def _ok_redis(_url: str) -> str:
        return "ok"

    monkeypatch.setattr(health_module, "_check_db", _ok_db)
    monkeypatch.setattr(health_module, "_check_redis", _ok_redis)


@pytest.mark.fast
async def test_health_returns_200(client: httpx.AsyncClient) -> None:
    """Liveness probe responds 200 with version metadata."""
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


@pytest.mark.fast
async def test_ready_returns_200_when_checks_pass(client: httpx.AsyncClient) -> None:
    """Readiness probe returns 200 when DB + Redis probes both return 'ok'."""
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"db": "ok", "redis": "ok"}


@pytest.mark.fast
async def test_ready_returns_503_when_db_fails(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness flips to 503 when the DB probe reports 'fail'."""

    async def _fail_db(_url: str) -> str:
        return "fail"

    monkeypatch.setattr(health_module, "_check_db", _fail_db)
    response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["db"] == "fail"


@pytest.mark.fast
async def test_ready_returns_503_when_redis_fails(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness flips to 503 when the Redis probe reports 'fail'."""

    async def _fail_redis(_url: str) -> str:
        return "fail"

    monkeypatch.setattr(health_module, "_check_redis", _fail_redis)
    response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"] == "fail"


@pytest.mark.fast
async def test_health_requires_no_auth(client: httpx.AsyncClient) -> None:
    """No Authorization header → still 200 (probes are public)."""
    response = await client.get("/health")
    assert response.status_code == 200
