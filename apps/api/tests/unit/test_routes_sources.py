"""Unit tests for ``POST /sources/admin_bootstrap``.

The admin-bypass endpoint creates a ``sources`` + ``source_identity``
row without an invite code, for callers whose Telegram id is in
``Settings.telegram_admin_chat_ids``. Idempotent on repeat calls.
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
ADMIN_ID = 151594111
NON_ADMIN_ID = 222
SECOND_ADMIN_ID = 333


@dataclass
class _FakeSourceRepo:
    sources: dict[str, SourceRecord] = field(default_factory=dict)

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        return self.sources.get(source_id)

    async def insert(
        self,
        *,
        source_id: str,
        display_name: str | None,
        kind: str,
        consent_version: str,
    ) -> SourceRecord:
        now = datetime.now(tz=UTC)
        rec = SourceRecord(
            source_id=source_id,
            display_name=display_name,
            kind=kind,
            reliability="F",
            credibility=6,
            consent_version=consent_version,
            created_at=now,
            updated_at=now,
        )
        self.sources[source_id] = rec
        return rec


@dataclass
class _FakeIdentityRepo:
    by_tg_id: dict[int, str] = field(default_factory=dict)

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        return self.by_tg_id.get(tg_user_id)

    async def insert(
        self,
        *,
        source_id: str,
        tg_user_id: int,
        tg_handle: str | None,
    ) -> None:
        self.by_tg_id[tg_user_id] = source_id


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
        telegram_admin_chat_ids=[ADMIN_ID, SECOND_ADMIN_ID],  # type: ignore[arg-type]
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


async def test_admin_bootstrap_admin_creates_source(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    response = await client.post(
        "/sources/admin_bootstrap",
        json={"tg_user_id": ADMIN_ID, "tg_handle": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["source_id"], str)
    assert body["source_id"].startswith("tg:")
    assert body["display_name_prompt"] is True
    assert fake_session.committed
    # The source row is marked kind='admin'.
    created = next(iter(fake_session.sources.sources.values()))
    assert created.kind == "admin"


async def test_admin_bootstrap_non_admin_403s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    response = await client.post(
        "/sources/admin_bootstrap",
        json={"tg_user_id": NON_ADMIN_ID, "tg_handle": "intruder"},
        headers=auth_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not an admin"
    assert not fake_session.sources.sources
    assert not fake_session.committed


async def test_admin_bootstrap_idempotent_for_same_admin(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A second call from the same admin returns their existing source_id."""
    from merlin_core.domain.source_id import make_source_id

    existing_source_id = make_source_id(ADMIN_ID)
    fake_session.identities.by_tg_id[ADMIN_ID] = existing_source_id
    fake_session.sources.sources[existing_source_id] = SourceRecord(
        source_id=existing_source_id,
        display_name="operator",
        kind="admin",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    response = await client.post(
        "/sources/admin_bootstrap",
        json={"tg_user_id": ADMIN_ID, "tg_handle": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["source_id"] == existing_source_id
    assert response.json()["display_name_prompt"] is False
    # No duplicate row inserted.
    assert len(fake_session.sources.sources) == 1
