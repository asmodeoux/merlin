"""Live-DB integration test for the invite lifecycle.

The unit suite in ``apps/api/tests/unit/test_routes_invites.py`` covers
the route's contract with in-memory fakes — fast, but never hits the
SQL layer. This module spins up a real Postgres via ``testcontainers``,
applies the Alembic schema, and walks the full lifecycle:

1. Admin mints a code  (``POST /invites``)
2. Alt user redeems it (``POST /invites/redeem``)
3. Idempotent re-redeem returns the existing source_id
4. Admin revokes a different code (``POST /invites/revoke``)
5. Revoked code can no longer be redeemed

Each assertion checks the HTTP wire shape AND the DB row, so a
regression in either layer fails this test.

Marked ``integration`` + ``slow``: ``make test-int`` runs this;
``make ci-fast`` skips it. Docker daemon must be reachable for the
testcontainer to boot.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer

ADMIN_TG_ID = 151594111
ALT_TG_ID = 222222222
TEST_TOKEN = "test-internal-token"

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _async_dsn(sync_dsn: str) -> str:
    """``testcontainers`` returns ``postgresql+psycopg2://...``; flip the
    driver to ``asyncpg`` so SQLAlchemy's async engine accepts it."""
    return (
        sync_dsn.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        .replace("postgresql://", "postgresql+asyncpg://")
        .replace("postgresql+psycopg://", "postgresql+asyncpg://")
    )


def _apply_alembic_migrations(async_dsn: str) -> None:
    """Apply every Alembic migration head against the live container.

    We use ``alembic.command.upgrade`` programmatically rather than
    shelling out — avoids subprocess plumbing and keeps the migration
    output inside the test process. The DSN must already be the
    asyncpg variant because ``alembic/env.py`` uses
    ``async_engine_from_config``; a sync ``psycopg2`` URL lands on the
    wrong dialect (the import-time ``import psycopg2`` blows up
    because we don't depend on psycopg2 at runtime).
    """
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[4]
    alembic_dir = repo_root / "packages" / "python" / "merlin_infrastructure" / "alembic"
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", async_dsn)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    """Module-scoped Postgres — one container for every test in this file.

    pgvector isn't required by the current schema (vector columns land
    in a later sprint), so vanilla ``postgres:16`` is enough. pgcrypto
    is shipped in the official image and the migration enables it
    idempotently.
    """
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def migrated_dsn(postgres_container: PostgresContainer) -> str:
    """Apply the schema once for the whole module; return the async DSN.

    Schema is invariant across tests in this file — we reset *data*
    (truncate the tables) between tests rather than re-migrating.
    """
    sync_dsn = postgres_container.get_connection_url()
    async_dsn = _async_dsn(sync_dsn)
    _apply_alembic_migrations(async_dsn)
    return async_dsn


@pytest.fixture
async def reset_data(migrated_dsn: str) -> AsyncIterator[None]:
    """Truncate the invite + source tables before each test.

    Order matters because of FKs: invites → source_identity → sources.
    ``CASCADE`` would also work but is heavier; the explicit order is
    cheap and audits cleanly.
    """
    engine = create_async_engine(migrated_dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE invites CASCADE"))
            await conn.execute(text("TRUNCATE source_identity CASCADE"))
            await conn.execute(text("TRUNCATE sources CASCADE"))
    finally:
        await engine.dispose()
    yield


@pytest.fixture
async def client(
    migrated_dsn: str,
    reset_data: None,
) -> AsyncIterator[httpx.AsyncClient]:
    """In-process FastAPI client wired against the live container DSN.

    Two overrides are needed:

    * ``get_settings`` for the admin-allowlist check the route does
      inline (``Depends(get_settings)``).
    * ``get_session`` for the DSN — the default ``get_session`` calls
      ``get_settings()`` *directly* (not via ``Depends``) because it's
      a generator, so the FastAPI override on ``get_settings``
      wouldn't propagate. We replace ``get_session`` with one that
      builds an engine against the testcontainer DSN.
    """
    test_settings = Settings(
        environment="test",
        database_url=migrated_dsn,
        merlin_api_internal_token=TEST_TOKEN,
        telegram_admin_chat_ids=[ADMIN_TG_ID],  # type: ignore[arg-type]
        sentry_dsn="",
    )
    engine = create_async_engine(migrated_dsn)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_session] = _override_get_session

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(migrated_dsn: str) -> AsyncIterator[AsyncSession]:
    """Direct async session for DB-level assertions.

    Kept on its OWN engine (separate from the one the FastAPI app uses
    in the ``client`` fixture) so cross-engine commit visibility is
    exercised against the real Postgres READ COMMITTED contract.

    ``expire_on_commit`` is left at the default so calling
    ``db_session.expire_all()`` after API mutations actually clears the
    identity map — otherwise re-fetching a previously-seen row hands
    back the stale Python object with pre-mutation attributes (the
    SQLAlchemy identity-map gotcha that bit this test on first write).
    """
    engine = create_async_engine(migrated_dsn)
    try:
        async with AsyncSession(engine) as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _bootstrap_admin(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """Onboard the admin so the ``created_by_source_id`` FK is satisfied."""
    response = await client.post(
        "/sources/admin_bootstrap",
        json={"tg_user_id": ADMIN_TG_ID, "tg_handle": "operator"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["source_id"])


async def _create_invite(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    expires_in_days: int = 14,
) -> dict[str, Any]:
    response = await client.post(
        "/invites",
        json={"tg_user_id": ADMIN_TG_ID, "expires_in_days": expires_in_days},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Tests — full invite lifecycle against live Postgres.
# ---------------------------------------------------------------------------


async def test_full_lifecycle_admin_creates_alt_redeems(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """The two-account happy path the user is about to run live.

    1. Admin runs /start (bootstrap). Source row lands as kind='admin'.
    2. Admin runs /invite. Invite row lands with created_by FK pointing
       at the admin's source_id.
    3. Alt user runs /start, pastes the code. New source row lands as
       kind='user', source_identity row lands with the alt's tg_user_id,
       invite row gets redeemed_at + redeemed_by_source_id set.
    4. Alt user re-pastes the same code (the bot's restart-after-flow
       scenario). API returns their existing source_id without burning
       a new invite.
    """
    from merlin_infrastructure.db.models import (
        Invite,
        Source,
        SourceIdentity,
    )

    admin_source_id = await _bootstrap_admin(client, auth_headers)
    assert admin_source_id.startswith("tg:")

    invite = await _create_invite(client, auth_headers)
    code = invite["code"]
    assert len(code) >= 8

    # DB-level sanity: one invite row, FK points at the admin.
    inv_row = (await db_session.execute(select(Invite).where(Invite.code == code))).scalar_one()
    assert inv_row.created_by_source_id == admin_source_id
    assert inv_row.redeemed_at is None
    assert inv_row.revoked_at is None

    # Alt user redeems.
    redeem = await client.post(
        "/invites/redeem",
        json={"code": code, "tg_user_id": ALT_TG_ID, "tg_handle": "alt_tester"},
        headers=auth_headers,
    )
    assert redeem.status_code == 200, redeem.text
    body = redeem.json()
    assert body["source_id"].startswith("tg:")
    assert body["source_id"] != admin_source_id
    assert body["display_name_prompt"] is True
    alt_source_id = body["source_id"]

    # Invalidate the identity-map so subsequent SELECTs re-query Postgres
    # for the freshly-committed rows — without this, fetching the
    # invite row a second time hands back the cached pre-redeem Python
    # object.
    db_session.expire_all()

    # The source row landed with the zeroed Admiralty defaults.
    src = (
        await db_session.execute(select(Source).where(Source.source_id == alt_source_id))
    ).scalar_one()
    assert src.kind == "user"
    assert src.reliability == "F"
    assert src.credibility == 6
    assert src.consent_version == "v0"
    assert src.display_name is None  # set by the bot's next step

    # The identity row landed with the raw TG id (PII boundary).
    identity = (
        await db_session.execute(
            select(SourceIdentity).where(SourceIdentity.source_id == alt_source_id),
        )
    ).scalar_one()
    assert identity.tg_user_id == ALT_TG_ID
    assert identity.tg_handle == "alt_tester"

    # The invite was atomically marked redeemed.
    inv_row = (await db_session.execute(select(Invite).where(Invite.code == code))).scalar_one()
    assert inv_row.redeemed_at is not None
    assert inv_row.redeemed_by_source_id == alt_source_id

    # Idempotent re-redeem: same alt user pasting the same code returns
    # their existing source_id without consuming a new code (none would
    # be available anyway — but we want the contract pinned even when
    # one is, see step (4) of the docstring).
    second = await client.post(
        "/invites/redeem",
        json={"code": code, "tg_user_id": ALT_TG_ID, "tg_handle": "alt_tester"},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json()["source_id"] == alt_source_id


async def test_unknown_code_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The "random code accepted" hole the user originally flagged —
    pinned end-to-end against the real lookup query."""
    response = await client.post(
        "/invites/redeem",
        json={"code": "completely-made-up-code", "tg_user_id": ALT_TG_ID},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_redeem_concurrent_only_one_wins(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Race-safety: two distinct TG users redeeming the same code in
    parallel must produce one 200 and one 4xx (the loser maps to either
    404 from the WHERE-redeemed_at-IS-NULL filter OR 409 from the
    ``InviteAlreadyRedeemedError`` race). The DB ends with exactly one
    redemption recorded."""
    from merlin_infrastructure.db.models import Invite

    await _bootstrap_admin(client, auth_headers)
    invite = await _create_invite(client, auth_headers)
    code = invite["code"]

    user_a, user_b = 333333333, 444444444
    results = await asyncio.gather(
        client.post(
            "/invites/redeem",
            json={"code": code, "tg_user_id": user_a, "tg_handle": "a"},
            headers=auth_headers,
        ),
        client.post(
            "/invites/redeem",
            json={"code": code, "tg_user_id": user_b, "tg_handle": "b"},
            headers=auth_headers,
        ),
    )
    statuses = sorted(r.status_code for r in results)
    # One winner (200) + one loser. The loser is either 404 (the
    # SELECT-then-conditional-UPDATE ordering catches it on the SELECT)
    # or 409 (race-loss on the UPDATE itself).
    assert statuses[0] == 200
    assert statuses[1] in (404, 409)

    db_session.expire_all()  # see test_full_lifecycle for the cache rationale
    inv_row = (await db_session.execute(select(Invite).where(Invite.code == code))).scalar_one()
    assert inv_row.redeemed_at is not None
    # And only one identity row landed (the loser's race-lose path
    # rolls back its source + identity INSERTs; the loser's 404 path
    # never INSERTs in the first place). Excluding the admin's own
    # identity row by checking sources.kind.
    user_identities = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM source_identity si "
                "JOIN sources s USING (source_id) WHERE s.kind = 'user'",
            ),
        )
    ).scalar_one()
    assert user_identities == 1


async def test_admin_can_revoke_then_redeem_is_blocked(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Revoke is the missing operational lever — confirm it actually
    invalidates the code at the DB level."""
    from merlin_infrastructure.db.models import Invite

    await _bootstrap_admin(client, auth_headers)
    invite = await _create_invite(client, auth_headers)
    code = invite["code"]

    revoke = await client.post(
        "/invites/revoke",
        json={"code": code, "tg_user_id": ADMIN_TG_ID},
        headers=auth_headers,
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["code"] == code

    # DB-level: the row is marked revoked.
    db_session.expire_all()
    inv_row = (await db_session.execute(select(Invite).where(Invite.code == code))).scalar_one()
    assert inv_row.revoked_at is not None

    # Subsequent redeem hits the same 404 path as unknown codes.
    redeem = await client.post(
        "/invites/redeem",
        json={"code": code, "tg_user_id": ALT_TG_ID, "tg_handle": "alt"},
        headers=auth_headers,
    )
    assert redeem.status_code == 404


async def test_expired_invite_cannot_be_redeemed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Expiry is enforced at the SELECT layer (``expires_at > now()``).

    We can't ask the API to mint an already-expired invite, so we
    rewind ``expires_at`` directly on the row. This is the only path
    that exercises the expiry filter against real Postgres time."""

    await _bootstrap_admin(client, auth_headers)
    invite = await _create_invite(client, auth_headers)
    code = invite["code"]

    # Backdate the row.
    await db_session.execute(
        text("UPDATE invites SET expires_at = :t WHERE code = :c").bindparams(
            t=datetime(2000, 1, 1, tzinfo=UTC),
            c=code,
        ),
    )
    await db_session.commit()

    response = await client.post(
        "/invites/redeem",
        json={"code": code, "tg_user_id": ALT_TG_ID, "tg_handle": "alt"},
        headers=auth_headers,
    )
    assert response.status_code == 404
