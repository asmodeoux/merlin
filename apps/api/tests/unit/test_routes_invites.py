"""Unit tests for ``POST /invites/redeem`` + ``POST /invites``.

The route handlers were rewritten in Sprint 1 to talk to real
repositories instead of returning a random UUID. These tests verify
the new wiring without standing up Postgres — we override
``get_session`` to yield a tiny in-memory fake whose repositories are
hand-written stand-ins. That keeps the test fast and pinned to the
*route's* contract (admin check, idempotency, 404 handling, etc.)
rather than the SQL layer (which gets its own integration tests).
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
from merlin_core.application.ports import InviteRecord, SourceRecord

TEST_TOKEN = "test-internal-token"
ADMIN_ID = 151594111
NON_ADMIN_ID = 999999


# ---------------------------------------------------------------------------
# Tiny in-memory stand-ins for the three repositories. Returned from the
# fake session by attribute — the route imports the concrete repository
# classes inside the function body, so we monkey-patch those names too.
# ---------------------------------------------------------------------------


@dataclass
class _FakeInviteRepo:
    invites: dict[str, InviteRecord] = field(default_factory=dict)
    redeem_calls: list[tuple[UUID, str]] = field(default_factory=list)
    revoke_calls: list[str] = field(default_factory=list)

    async def get_redeemable_by_code(self, code: str) -> InviteRecord | None:
        rec = self.invites.get(code)
        if rec is None or not rec.is_redeemable:
            return None
        return rec

    async def insert(
        self,
        *,
        code: str,
        created_by_source_id: str | None,
        expires_at: datetime,
    ) -> InviteRecord:
        if code in self.invites:
            raise ValueError(f"invite code already exists: {code}")
        rec = InviteRecord(
            id=uuid4(),
            code=code,
            created_by_source_id=created_by_source_id,
            created_at=datetime.now(tz=UTC),
            expires_at=expires_at,
            redeemed_at=None,
            redeemed_by_source_id=None,
            revoked_at=None,
        )
        self.invites[code] = rec
        return rec

    async def mark_redeemed(
        self,
        *,
        invite_id: UUID,
        redeemed_by_source_id: str,
        redeemed_at: datetime,
    ) -> bool:
        self.redeem_calls.append((invite_id, redeemed_by_source_id))
        for code, rec in self.invites.items():
            if rec.id == invite_id and rec.is_redeemable:
                self.invites[code] = InviteRecord(
                    id=rec.id,
                    code=rec.code,
                    created_by_source_id=rec.created_by_source_id,
                    created_at=rec.created_at,
                    expires_at=rec.expires_at,
                    redeemed_at=redeemed_at,
                    redeemed_by_source_id=redeemed_by_source_id,
                    revoked_at=None,
                )
                return True
        return False

    async def revoke_by_code(self, *, code: str, revoked_at: datetime) -> bool:
        self.revoke_calls.append(code)
        rec = self.invites.get(code)
        if rec is None or not rec.is_redeemable:
            return False
        self.invites[code] = InviteRecord(
            id=rec.id,
            code=rec.code,
            created_by_source_id=rec.created_by_source_id,
            created_at=rec.created_at,
            expires_at=rec.expires_at,
            redeemed_at=rec.redeemed_at,
            redeemed_by_source_id=rec.redeemed_by_source_id,
            revoked_at=revoked_at,
        )
        return True


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
    """Just enough surface for the route to call ``commit`` / ``rollback``."""

    invites: _FakeInviteRepo = field(default_factory=_FakeInviteRepo)
    sources: _FakeSourceRepo = field(default_factory=_FakeSourceRepo)
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    committed: bool = False
    rolled_back: bool = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def get(self, *_args: Any, **_kw: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixture wiring — overrides the session + monkey-patches the repository
# classes the route imports inline.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
        telegram_admin_chat_ids=[ADMIN_ID],  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(
    fake_session: _FakeSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Test client whose ``get_session`` yields the fake session and whose
    inline ``InviteRepository`` / ``SourceRepository`` /
    ``SourceIdentityRepository`` imports resolve to our fakes."""
    # Pre-import the modules whose classes the route handlers look up
    # at call time, so monkeypatch.setattr can target the exact names.
    from merlin_infrastructure.db import repositories as _repos_mod

    monkeypatch.setattr(_repos_mod, "InviteRepository", lambda _s: fake_session.invites)
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


# ---------------------------------------------------------------------------
# POST /invites — admin issuance.
# ---------------------------------------------------------------------------


async def test_create_invite_admin_succeeds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    response = await client.post(
        "/invites",
        json={"tg_user_id": ADMIN_ID, "expires_in_days": 7},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["code"]) >= 8
    assert "expires_at" in body
    assert fake_session.committed
    assert len(fake_session.invites.invites) == 1


async def test_create_invite_non_admin_403s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    response = await client.post(
        "/invites",
        json={"tg_user_id": NON_ADMIN_ID},
        headers=auth_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not an admin"
    assert not fake_session.invites.invites


# ---------------------------------------------------------------------------
# POST /invites/redeem — user-facing redemption.
# ---------------------------------------------------------------------------


async def test_redeem_invite_random_code_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The user's specific concern: a "random" code must NOT succeed.

    Sprint 0 stub returned 200 + a fake source_id for any code that
    wasn't literally 'invalid' or 'expired'. That's the security hole
    we closed. A made-up code now goes through the real
    ``invites`` lookup → 404.
    """
    response = await client.post(
        "/invites/redeem",
        json={"code": "any-random-string-here", "tg_user_id": 4242},
        headers=auth_headers,
    )
    assert response.status_code == 404, response.text


async def test_redeem_invite_real_code_succeeds_and_marks_redeemed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """The happy path: admin pre-seeds a code; alt user redeems it."""
    fake_session.invites.invites["VALID_CODE"] = InviteRecord(
        id=uuid4(),
        code="VALID_CODE",
        created_by_source_id=None,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        redeemed_at=None,
        redeemed_by_source_id=None,
        revoked_at=None,
    )
    response = await client.post(
        "/invites/redeem",
        json={"code": "VALID_CODE", "tg_user_id": 1234567, "tg_handle": "alt_account"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # source_id must be the opaque ``tg:<sha>`` string, NOT a UUID.
    assert isinstance(body["source_id"], str)
    assert body["source_id"].startswith("tg:")
    assert body["display_name_prompt"] is True
    # The invite is now consumed.
    assert fake_session.invites.invites["VALID_CODE"].redeemed_at is not None
    assert fake_session.committed


async def test_redeem_invite_idempotent_for_same_tg_user(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A user who pastes the same code twice gets back their existing
    source_id rather than burning a fresh row + invite."""
    from merlin_core.domain.source_id import make_source_id

    tg_user_id = 1234567
    existing_source_id = make_source_id(tg_user_id)
    fake_session.identities.by_tg_id[tg_user_id] = existing_source_id
    fake_session.sources.sources[existing_source_id] = SourceRecord(
        source_id=existing_source_id,
        display_name="alt_account",
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    response = await client.post(
        "/invites/redeem",
        json={"code": "ANY", "tg_user_id": tg_user_id},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["source_id"] == existing_source_id
    # display_name already set → no prompt.
    assert response.json()["display_name_prompt"] is False
    # No new invite consumed; no commit needed.
    assert fake_session.invites.redeem_calls == []


# ---------------------------------------------------------------------------
# POST /invites/revoke — admin invalidation.
# ---------------------------------------------------------------------------


async def test_revoke_invite_admin_succeeds_and_marks_revoked(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Happy path: admin revokes a live code; row is marked revoked."""
    fake_session.invites.invites["LIVE_CODE"] = InviteRecord(
        id=uuid4(),
        code="LIVE_CODE",
        created_by_source_id=None,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        redeemed_at=None,
        redeemed_by_source_id=None,
        revoked_at=None,
    )

    response = await client.post(
        "/invites/revoke",
        json={"code": "LIVE_CODE", "tg_user_id": ADMIN_ID},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "LIVE_CODE"
    assert body.get("revoked_at")
    assert fake_session.invites.invites["LIVE_CODE"].revoked_at is not None
    assert fake_session.committed
    # The route used the revoke path, not the redeem path.
    assert fake_session.invites.revoke_calls == ["LIVE_CODE"]


async def test_revoke_invite_non_admin_403s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Non-admins can't reach the revoke surface."""
    fake_session.invites.invites["LIVE_CODE"] = InviteRecord(
        id=uuid4(),
        code="LIVE_CODE",
        created_by_source_id=None,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        redeemed_at=None,
        redeemed_by_source_id=None,
        revoked_at=None,
    )

    response = await client.post(
        "/invites/revoke",
        json={"code": "LIVE_CODE", "tg_user_id": NON_ADMIN_ID},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "not an admin"
    # The repo's revoke path was never touched.
    assert fake_session.invites.revoke_calls == []
    assert fake_session.invites.invites["LIVE_CODE"].revoked_at is None


async def test_revoke_invite_unknown_code_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/invites/revoke",
        json={"code": "DOES_NOT_EXIST", "tg_user_id": ADMIN_ID},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_revoke_invite_already_redeemed_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """An already-consumed code can't be revoked — the redemption
    already happened. Same 404 status as unknown-code (the three
    terminal states all collapse to ``no longer revocable``)."""
    fake_session.invites.invites["DEAD_CODE"] = InviteRecord(
        id=uuid4(),
        code="DEAD_CODE",
        created_by_source_id=None,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        redeemed_at=datetime.now(tz=UTC),
        redeemed_by_source_id="tg:somebody",
        revoked_at=None,
    )

    response = await client.post(
        "/invites/revoke",
        json={"code": "DEAD_CODE", "tg_user_id": ADMIN_ID},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_revoke_invite_already_revoked_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Revoking an already-revoked code is a no-op error from the
    admin's perspective — there's nothing to do, but it's not a
    fresh success either."""
    fake_session.invites.invites["KILLED_CODE"] = InviteRecord(
        id=uuid4(),
        code="KILLED_CODE",
        created_by_source_id=None,
        created_at=datetime.now(tz=UTC),
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        redeemed_at=None,
        redeemed_by_source_id=None,
        revoked_at=datetime.now(tz=UTC),
    )

    response = await client.post(
        "/invites/revoke",
        json={"code": "KILLED_CODE", "tg_user_id": ADMIN_ID},
        headers=auth_headers,
    )
    assert response.status_code == 404
