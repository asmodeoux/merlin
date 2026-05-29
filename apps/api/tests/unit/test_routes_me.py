"""Unit tests for ``GET /me`` and ``PATCH /me``.

* ``PATCH /me`` — display-name finalisation. Sprint-1 wired this endpoint
  against the ``sources`` + ``source_identity`` tables so the onboarding
  flow actually completes end-to-end (display names survive a bot
  restart). The route looks the calling source up by ``tg_user_id`` and
  404s if the source hasn't been onboarded yet.

* ``GET /me`` — open-call snapshot. Resolves ``tg_user_id`` →
  ``source_id`` via ``source_identity``, then projects open + resolved
  counts. Returns an honest empty-state when the user has no resolved
  calls (``accuracy_pct`` is ``None``, NOT 0.0 — the contract makes
  this Optional precisely so the bot can render "tracking your first
  resolutions" instead of a misleading zero).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings
from merlin_core.application.ports import ClaimRecord, SourceRecord

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
class _FakeClaimRepo:
    open_by_source: dict[str, list[ClaimRecord]] = field(default_factory=dict)
    resolved_count_by_source: dict[str, int] = field(default_factory=dict)
    leaderboard_rows: list[Any] = field(default_factory=list)

    async def list_open_by_source(self, source_id: str) -> list[ClaimRecord]:
        return list(self.open_by_source.get(source_id, []))

    async def count_resolved_by_source(self, source_id: str) -> int:
        return self.resolved_count_by_source.get(source_id, 0)

    async def leaderboard(self, _window: str) -> list[Any]:
        return list(self.leaderboard_rows)


@dataclass
class _FakeSession:
    sources: _FakeSourceRepo = field(default_factory=_FakeSourceRepo)
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    claims: _FakeClaimRepo = field(default_factory=_FakeClaimRepo)
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
    monkeypatch.setattr(_repos_mod, "ClaimRepository", lambda _s: fake_session.claims)

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


def _seed_onboarded(session: _FakeSession, *, tg_user_id: int, source_id: str) -> None:
    """Convenience: seed a fully-onboarded user with no claims yet."""
    session.identities.by_tg_id[tg_user_id] = source_id
    session.sources.sources[source_id] = SourceRecord(
        source_id=source_id,
        display_name="Yuri",
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _claim(*, claim_id: UUID, source_id: str, **overrides: Any) -> ClaimRecord:
    """Build a ClaimRecord with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": claim_id,
        "source_id": source_id,
        "asset_coingecko_id": "ethereum",
        "asset_symbol": "ETH",
        "asset_name": "Ethereum",
        "direction": "long",
        "horizon": "14d",
        "conviction": "strong",
        "thesis_text": "Vitalik staking news",
        "asset_query": "ETH",
        "submitted_at": datetime.now(tz=UTC),
        "resolves_at": datetime.now(tz=UTC) + timedelta(days=14),
        "parsed_via": "freeform_llm",
        "extracted_fields": {},
        "parse_metadata": {},
    }
    defaults.update(overrides)
    return ClaimRecord(**defaults)


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


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


async def test_get_me_returns_open_calls_and_display_name(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Happy path: onboarded user with one open call — the route must
    project the source's display name + the open call's wire-format
    fields."""
    src_id = "tg:0000111122223333abcdabcd00001111"
    _seed_onboarded(fake_session, tg_user_id=42, source_id=src_id)
    claim_id = uuid4()
    fake_session.claims.open_by_source[src_id] = [
        _claim(claim_id=claim_id, source_id=src_id),
    ]
    fake_session.claims.resolved_count_by_source[src_id] = 0

    response = await client.get("/me", params={"tg_user_id": 42}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == "Yuri"
    assert len(body["calls_open"]) == 1
    open_call = body["calls_open"][0]
    assert open_call["asset_symbol"] == "ETH"
    assert open_call["direction"] == "long"
    assert open_call["horizon"] == "14d"
    assert UUID(open_call["claim_id"]) == claim_id
    assert body["calls_resolved_total"] == 0
    # Honest empty-state — never 0.0% accuracy when nothing has resolved.
    assert body["accuracy_pct"] is None
    assert body["rank"] is None


async def test_get_me_empty_state(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Onboarded user with zero claims — the route returns an empty
    list (NOT a placeholder), and accuracy/rank stay null. The bot
    renders the "tracking your first resolutions" copy from these
    nulls."""
    src_id = "tg:" + "z" * 32
    _seed_onboarded(fake_session, tg_user_id=7, source_id=src_id)

    response = await client.get("/me", params={"tg_user_id": 7}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["calls_open"] == []
    assert body["calls_resolved_recent"] == []
    assert body["calls_resolved_total"] == 0
    assert body["accuracy_pct"] is None
    assert body["rank"] is None


async def test_get_me_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A user who hasn't been onboarded gets a 404 (the bot reads
    this and points them at /start). Critical: we MUST NOT return an
    empty MeResponse here — that would tell unboarded users they have
    "no open calls" rather than telling them they aren't signed up."""
    response = await client.get("/me", params={"tg_user_id": 12345}, headers=auth_headers)
    assert response.status_code == 404


async def test_get_me_requires_tg_user_id_param(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Without ``tg_user_id`` the API has no way to identify the
    caller — must 422, never silently default to a magic source."""
    response = await client.get("/me", headers=auth_headers)
    assert response.status_code == 422
