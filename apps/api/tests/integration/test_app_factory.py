"""Integration-level smoke — the FastAPI app starts and serves /docs."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
async def test_openapi_json_served(client: httpx.AsyncClient) -> None:
    """The schema endpoint serves a non-empty document."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Merlin API"


@pytest.mark.integration
async def test_docs_served(client: httpx.AsyncClient) -> None:
    """/docs serves Swagger UI."""
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()


@pytest.mark.integration
async def test_redoc_served(client: httpx.AsyncClient) -> None:
    """/redoc serves ReDoc."""
    response = await client.get("/redoc")
    assert response.status_code == 200
    assert "redoc" in response.text.lower()
