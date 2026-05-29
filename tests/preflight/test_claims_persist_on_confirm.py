"""Preflight: the ``POST /claims`` route MUST persist before enqueueing.

Sprint-1 live bug we want to catch in CI forever:

* ``POST /claims`` returned 201 + a claim_id, the bot showed the
  receipt, but the underlying ``claims`` row was never INSERTed (the
  use case docstring said "5 INSERTs" — the actual code path was
  enqueue-only). Anything ``/me`` returned would have been a lie.

This preflight test asserts at the module / route level — no Postgres
needed — that the route imports the persistence repository AND calls
``create`` on it. It catches the regression class "route says it
persists but doesn't" rather than waiting for an integration test.

Two layers of check:

1. **Source grep**: the route file must reference ``ClaimRepository``
   and call ``.create``. A naive but reliable defence against the
   route being silently rewritten to skip persistence.

2. **Behavioural**: spin the route up with fake ports + a fake
   session, hit ``POST /claims``, assert the fake claim repo
   recorded a ``create`` call. Catches the case where the route
   imports the symbol but doesn't actually call it.

Both checks together close the loop the original integration test
gap missed.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAIMS_ROUTE = REPO_ROOT / "apps" / "api" / "merlin_api" / "routes" / "claims.py"


# ---------------------------------------------------------------------------
# Layer 1 — source grep
# ---------------------------------------------------------------------------


def test_claims_route_imports_claim_repository() -> None:
    """The route module must reference ``ClaimRepository``.

    If a future refactor removes the import, this test catches it
    immediately — before the bot starts lying about persisted claims.
    """
    source = CLAIMS_ROUTE.read_text(encoding="utf-8")
    assert "ClaimRepository" in source, (
        "apps/api/merlin_api/routes/claims.py no longer references "
        "ClaimRepository — the /claims route would persist nothing. "
        "Restore the import + the .create() call."
    )


def test_claims_route_calls_create_method() -> None:
    """The route must actually invoke ``.create(...)`` on the repo.

    Catches the "imported but unused" failure mode where the symbol
    sticks around but the route's been silently rewritten.
    """
    source = CLAIMS_ROUTE.read_text(encoding="utf-8")
    # ``claim_repo.create`` is the canonical shape; ``.create(`` is
    # the broader guard against renames inside the route.
    assert re.search(r"\.create\(", source), (
        "apps/api/merlin_api/routes/claims.py no longer calls "
        "ClaimRepository.create — the /claims route is skipping "
        "persistence. Restore the call inside submit_claim()."
    )


def test_claims_route_persists_before_enqueueing() -> None:
    """Persist-before-enqueue order check — source-level.

    The route must ``ClaimRepository.create`` *before* the queue's
    ``enqueue_at`` call. The reverse order is the historical bug
    (enqueue first, then INSERT; if the INSERT fails the worker
    fires on a phantom).
    """
    source = CLAIMS_ROUTE.read_text(encoding="utf-8")
    create_idx = source.find(".create(")
    enqueue_idx = source.find("enqueue_at(")
    assert create_idx != -1, "missing .create( call"
    assert enqueue_idx != -1, "missing enqueue_at( call"
    assert create_idx < enqueue_idx, (
        ".create() must run BEFORE enqueue_at() so the row exists by "
        "the time the worker fires. The reverse order is the "
        "historical Sprint-1 bug."
    )


# ---------------------------------------------------------------------------
# Layer 2 — behavioural (hit the route with fakes)
# ---------------------------------------------------------------------------


@dataclass
class _FakeIdentityRepo:
    by_tg_id: dict[int, str] = field(default_factory=dict)

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        return self.by_tg_id.get(tg_user_id)


@dataclass
class _FakeClaimRepo:
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        # Project a minimal record shape (route doesn't read it).
        return type(
            "FakeRecord",
            (),
            {"id": kwargs["claim_id"], "source_id": kwargs["source_id"]},
        )()


@dataclass
class _FakeSession:
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    claims: _FakeClaimRepo = field(default_factory=_FakeClaimRepo)
    committed: bool = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None: ...

    async def get(self, *_a: Any, **_kw: Any) -> None:
        return None


class _NoopQueue:
    def enqueue_at(self, *_a: Any, **_kw: Any) -> None:
        pass


class _NoopDrafter:
    async def draft(self, *_a: Any, **_kw: Any) -> Any:
        raise NotImplementedError


@dataclass
class _FakeMarketData:
    async def search_assets(self, *_a: Any, **_kw: Any) -> list[Any]:
        return []

    async def get_asset_metadata(self, coingecko_id: str) -> Any:
        from merlin_core.application.ports import AssetMetadata

        return AssetMetadata(
            coingecko_id=coingecko_id,
            symbol="ETH",
            name="Ethereum",
        )


class _NoopNotifier:
    async def notify(self, *_a: Any, **_kw: Any) -> None:
        pass


@pytest.fixture
async def client_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, _FakeSession]]:
    """Build a FastAPI app wired to in-memory fakes."""
    from merlin_api.app import create_app
    from merlin_api.config import Settings
    from merlin_api.deps import Ports, get_ports, get_session, get_settings
    from merlin_infrastructure.db import repositories as _repos_mod

    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:" + "a" * 32

    monkeypatch.setattr(
        _repos_mod,
        "SourceIdentityRepository",
        lambda _s: session.identities,
    )
    monkeypatch.setattr(_repos_mod, "ClaimRepository", lambda _s: session.claims)

    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield session

    app = create_app()
    settings = Settings(
        environment="test",
        merlin_api_internal_token="test-token",
        sentry_dsn="",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ports] = lambda: Ports(
        drafter=_NoopDrafter(),
        market_data=_FakeMarketData(),
        queue=_NoopQueue(),
        notifier=_NoopNotifier(),
    )
    app.dependency_overrides[get_session] = _fake_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, session


@pytest.mark.asyncio
async def test_submit_claim_actually_creates_a_row(
    client_with_fakes: tuple[httpx.AsyncClient, _FakeSession],
) -> None:
    """Posting to /claims must record a ``create`` call.

    Catches the import-but-don't-call regression — the route could
    grep-pass the layer-1 check while still skipping the actual call
    in some refactor. This is the behavioural defence.
    """
    client, session = client_with_fakes
    payload = {
        "coingecko_id": "ethereum",
        "direction": "long",
        "horizon": "14d",
        "conviction": "strong",
        "thesis_text": "ETH long 2 weeks",
        "tg_user_id": 42,
        "asset_query": "ETH",
        "extracted_fields": {"schema_version": 1},
        "parse_metadata": {"parsed_via": "freeform_llm"},
    }
    response = await client.post(
        "/claims",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 201, response.text
    assert len(session.claims.create_calls) == 1, (
        "POST /claims did not call ClaimRepository.create — the route "
        "is skipping persistence. /me would show 0 open calls forever."
    )
    assert session.committed, "route must commit after persist + enqueue"
    # Smoke-check on the payload — ensures the fields the route
    # forwards are still aligned with the model.
    kwargs = session.claims.create_calls[0]
    assert kwargs["source_id"] == "tg:" + "a" * 32
    assert kwargs["asset_coingecko_id"] == "ethereum"
    assert kwargs["direction"] == "long"
    assert kwargs["horizon"] == "14d"
    assert kwargs["asset_query"] == "ETH"
    # ``submitted_at`` + ``resolves_at`` are timezone-aware datetimes
    # — the model column is timestamptz; naive datetimes would
    # silently land as "now() in local TZ" on Postgres.
    assert isinstance(kwargs["submitted_at"], datetime)
    assert kwargs["submitted_at"].tzinfo is not None
    assert kwargs["submitted_at"].tzinfo.utcoffset(kwargs["submitted_at"]) == UTC.utcoffset(
        kwargs["submitted_at"],
    )
