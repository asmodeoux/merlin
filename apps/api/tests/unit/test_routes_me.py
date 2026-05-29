"""Unit tests for ``PATCH /me`` — display-name finalisation.

Sprint 1 wired this endpoint against the ``sources`` + ``source_identity``
tables so the onboarding flow actually completes end-to-end (display
names survive a bot restart). The route looks the calling source up by
``tg_user_id`` and 404s if the source hasn't been onboarded yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings
from merlin_core.application.ports import SourceRecord

TEST_TOKEN = "test-internal-token"


@dataclass
class _FakeSourceRepo:
    sources: dict[str, SourceRecord] = field(default_factory=dict)

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        return self.sources.get(source_id)

    async def update_display_name(self, source_id: str, display_name: str) -> SourceRecord:
        now = datetime.now(tz=UTC)
        existing = self.sources[source_id]
        updated = SourceRecord(
            source_id=existing.source_id,
            display_name=display_name,
            kind=existing.kind,
            reliability=existing.reliability,
            credibility=existing.credibility,
            consent_version=existing.consent_version,
            created_at=existing.created_at,
            updated_at=now,
        )
        self.sources[source_id] = updated
        return updated


@dataclass
class _FakeIdentityRepo:
    by_tg_id: dict[int, str] = field(default_factory=dict)

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        return self.by_tg_id.get(tg_user_id)


@dataclass
class _FakeSession:
    sources: _FakeSourceRepo = field(default_factory=_FakeSourceRepo)
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    committed: bool = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None: ...

    async def get(self, *_args: Any, **_kw: Any) -> None:
        return None


@pytest.fixture
def fake_session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


@pytest.fixture
async def client(
    fake_session: _FakeSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    from merlin_infrastructure.db import repositories as _repos_mod

    monkeypatch.setattr(_repos_mod, "SourceRepository", lambda _s: fake_session.sources)
    monkeypatch.setattr(
        _repos_mod,
        "SourceIdentityRepository",
        lambda _s: fake_session.identities,
    )

    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield fake_session

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = _fake_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


async def test_patch_me_persists_display_name(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Seed an onboarded source, PATCH /me, assert the row was updated."""
    src_id = "tg:0000111122223333abcdabcd00001111"
    fake_session.identities.by_tg_id[42] = src_id
    fake_session.sources.sources[src_id] = SourceRecord(
        source_id=src_id,
        display_name=None,
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    response = await client.patch(
        "/me",
        json={"display_name": "Alice", "tg_user_id": 42},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    assert fake_session.sources.sources[src_id].display_name == "Alice"
    assert fake_session.committed


async def test_patch_me_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A user who hasn't been onboarded yet can't set a display name —
    the route MUST 404 rather than silently doing nothing."""
    response = await client.patch(
        "/me",
        json={"display_name": "Ghost", "tg_user_id": 9999},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert not fake_session.committed
