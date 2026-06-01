"""Unit tests for ``POST /claims/draft`` and ``POST /claims``.

* ``POST /claims/draft`` — the freeform-LLM extraction endpoint.
  Sprint-1 live bug: the route was searching CoinGecko with
  ``thesis_text`` (the full sentence) instead of the LLM-extracted
  ``asset_query`` (a short symbol). Obvious calls like *"ETH long 2
  weeks, Vitalik staking news"* landed with zero asset candidates.
  These tests pin the wiring contract:

  - The asset search runs against ``draft.asset_query``, NOT
    ``draft.thesis_text``.
  - When the LLM didn't surface an asset (``draft.asset_query is None``),
    the API does not call ``search_assets`` at all — the bot opens the
    picker instead.
  - LLM transport errors map to 503 and the bot falls back to the form.

* ``POST /claims`` — atomic commit. The route persists the claim row
  via ``ClaimRepository.create`` *before* enqueueing the resolution
  job. The opposite order (enqueue first, INSERT second) leaves the
  worker pointing at a phantom row when the INSERT fails — that's the
  latent bug the previous "no DB" path masked. These tests pin the
  order + the unknown-user 404.

All ports + the DB session are replaced with hand-rolled fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import Ports, get_ports, get_session, get_settings
from merlin_core.application.ports import (
    AssetMetadata,
    ClaimDrafterError,
    ClaimRecord,
    MarketDataError,
    PricePoint,
)
from merlin_core.domain import (
    AssetCandidate,
    ClaimDraft,
    ParseMetadata,
)

TEST_TOKEN = "test-internal-token"


# ---------------------------------------------------------------------------
# Fake adapters
# ---------------------------------------------------------------------------


@dataclass
class _FakeDrafter:
    """Returns a hard-coded ClaimDraft per call; records the input text."""

    response: ClaimDraft
    calls: list[str] = field(default_factory=list)
    raise_with: Exception | None = None

    async def draft(self, text: str) -> ClaimDraft:
        self.calls.append(text)
        if self.raise_with is not None:
            raise self.raise_with
        return self.response


@dataclass
class _FakeMarketData:
    """Records every ``search_assets`` query so the test can pin the contract.

    Also fakes ``get_price_at`` because ``POST /claims`` calls it
    at submit time to capture the entry-price snapshot. Tests that
    want to assert the failure path (CoinGecko down at submit) set
    ``price_raise_with``; tests that want a price set
    ``price_response``. The default is an arbitrary but recognisable
    fixture price so existing test paths don't need to seed one.
    """

    candidates: list[AssetCandidate] = field(default_factory=list)
    search_calls: list[str] = field(default_factory=list)
    raise_with: Exception | None = None
    metadata_response: AssetMetadata | None = None
    metadata_calls: list[str] = field(default_factory=list)
    price_response: PricePoint | None = None
    price_calls: list[tuple[str, datetime]] = field(default_factory=list)
    price_raise_with: Exception | None = None

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        self.search_calls.append(query)
        if self.raise_with is not None:
            raise self.raise_with
        return self.candidates

    async def get_asset_metadata(self, coingecko_id: str) -> AssetMetadata:
        self.metadata_calls.append(coingecko_id)
        if self.metadata_response is None:
            raise NotImplementedError(
                f"test forgot to seed get_asset_metadata for {coingecko_id!r}",
            )
        return self.metadata_response

    async def get_price_at(self, coingecko_id: str, when: datetime) -> PricePoint:
        self.price_calls.append((coingecko_id, when))
        if self.price_raise_with is not None:
            raise self.price_raise_with
        if self.price_response is not None:
            return self.price_response
        # Sensible default: $2,438.12 at the requested timestamp.
        # Tests that pin the price-capture wiring override this; tests
        # that don't care just want the route to succeed.
        return PricePoint(
            coingecko_id=coingecko_id,
            requested_at=when,
            returned_at=when,
            price_usd=2438.12,
        )


@dataclass
class _RecordingQueue:
    """Captures every ``enqueue_at`` so tests can assert ordering."""

    enqueues: list[tuple[Any, ...]] = field(default_factory=list)

    def enqueue_at(self, when: Any, fn: str, *args: Any, **kwargs: Any) -> None:
        self.enqueues.append((when, fn, args, kwargs))


class _NoopQueue:
    def enqueue_at(self, *_a: Any, **_kw: Any) -> None:
        pass


class _NoopNotifier:
    async def notify(self, *_a: Any, **_kw: Any) -> None:
        pass


@dataclass
class _FakeIdentityRepo:
    by_tg_id: dict[int, str] = field(default_factory=dict)

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        return self.by_tg_id.get(tg_user_id)


@dataclass
class _FakeClaimRepo:
    created: list[dict[str, Any]] = field(default_factory=list)
    create_calls_before_enqueue: list[bool] = field(default_factory=list)
    # The recording queue we share with the test, so create() can
    # capture "was the queue empty when I was called?" — that's how
    # we pin "INSERT before enqueue".
    queue_observer: _RecordingQueue | None = None

    async def create(self, **kwargs: Any) -> ClaimRecord:
        self.created.append(kwargs)
        if self.queue_observer is not None:
            # If the queue is still empty when we land here, the route
            # did INSERT-before-enqueue (correct). If the queue already
            # has an entry, the route enqueued first (the latent bug).
            self.create_calls_before_enqueue.append(
                len(self.queue_observer.enqueues) == 0,
            )
        return ClaimRecord(
            id=kwargs["claim_id"],
            source_id=kwargs["source_id"],
            asset_coingecko_id=kwargs["asset_coingecko_id"],
            asset_symbol=kwargs["asset_symbol"],
            asset_name=kwargs.get("asset_name"),
            direction=kwargs["direction"],
            horizon=kwargs["horizon"],
            conviction=kwargs["conviction"],
            thesis_text=kwargs["thesis_text"],
            asset_query=kwargs.get("asset_query"),
            submitted_at=kwargs["submitted_at"],
            resolves_at=kwargs["resolves_at"],
            parsed_via=kwargs["parsed_via"],
            extracted_fields=kwargs["extracted_fields"],
            parse_metadata=kwargs["parse_metadata"],
        )


@dataclass
class _FakeSession:
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    claims: _FakeClaimRepo = field(default_factory=_FakeClaimRepo)
    committed: bool = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None: ...

    async def get(self, *_args: Any, **_kw: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


def _build_app(
    *,
    drafter: _FakeDrafter,
    market: _FakeMarketData,
    queue: Any | None = None,
    session: _FakeSession | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> Any:
    """Build a FastAPI app with the given drafter + market data + queue.

    When a ``session`` + ``monkeypatch`` are supplied, the claim
    persistence path (``POST /claims``) is wired against the fake
    session's repos. Tests that exercise only ``/claims/draft`` can
    skip both.
    """
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )
    app.dependency_overrides[get_ports] = lambda: Ports(
        drafter=drafter,
        market_data=market,
        queue=queue or _NoopQueue(),
        notifier=_NoopNotifier(),
    )
    if session is not None:
        from merlin_infrastructure.db import repositories as _repos_mod

        assert monkeypatch is not None, (
            "session-backed tests must pass monkeypatch so the route's "
            "in-function repo imports can be swapped for fakes"
        )
        monkeypatch.setattr(
            _repos_mod,
            "SourceIdentityRepository",
            lambda _s: session.identities,
        )
        monkeypatch.setattr(_repos_mod, "ClaimRepository", lambda _s: session.claims)

        async def _fake_get_session() -> AsyncIterator[_FakeSession]:
            yield session

        app.dependency_overrides[get_session] = _fake_get_session
    return app


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _draft_with(*, asset_query: str | None, thesis: str) -> ClaimDraft:
    """A populated ClaimDraft fixture (direction/horizon/conviction landed,
    only asset varies)."""
    return ClaimDraft(
        asset_query=asset_query,
        direction="long",
        horizon="14d",
        conviction="strong",
        thesis_text=thesis,
        parse_metadata=ParseMetadata(
            parsed_via="freeform_llm",
            model="openai/gpt-5-mini",
            raw_user_turns=(thesis,),
            confidences={
                "asset_query": 0.95 if asset_query else 0.1,
                "direction": 0.95,
                "horizon": 0.9,
                "conviction": 0.85,
            },
            extracted_at=datetime.now(tz=UTC),
        ),
    )


@pytest.fixture
async def client_factory() -> AsyncIterator[Any]:
    """Yield a builder so each test wires its own (drafter, market)."""
    transports: list[httpx.ASGITransport] = []
    clients: list[httpx.AsyncClient] = []

    async def _build(
        drafter: _FakeDrafter,
        market: _FakeMarketData,
    ) -> httpx.AsyncClient:
        app = _build_app(drafter=drafter, market=market)
        transport = httpx.ASGITransport(app=app)
        transports.append(transport)
        ac = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(ac)
        return ac

    yield _build

    for c in clients:
        await c.aclose()


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


async def test_draft_searches_with_asset_query_not_thesis(
    client_factory: Any,
    auth_headers: dict[str, str],
) -> None:
    """The regression that motivated this file.

    User input: ``"ETH long 2 weeks, Vitalik announced a large upgrade
    to staking"``. LLM returns ``asset_query="ETH"``, thesis_text =
    full sentence. Previous behaviour: CoinGecko got the full sentence
    and returned nothing. Correct behaviour: CoinGecko gets ``"ETH"``
    and returns Ethereum as a candidate.
    """
    full_text = "ETH long 2 weeks, Vitalik announced a large upgrade to staking"
    drafter = _FakeDrafter(
        response=_draft_with(asset_query="ETH", thesis=full_text),
    )
    market = _FakeMarketData(
        candidates=[
            AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),
        ],
    )
    client = await client_factory(drafter, market)
    response = await client.post(
        "/claims/draft",
        json={"text": full_text},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    # The contract: search_assets is called exactly once, with the
    # LLM-extracted short symbol — NOT the verbatim thesis sentence.
    assert market.search_calls == ["ETH"], (
        f"market data was queried with {market.search_calls!r}; expected ['ETH']. "
        "The route MUST NOT fall back to draft.thesis_text — feeding the full "
        "sentence to CoinGecko /search returns 0 candidates for obvious calls."
    )
    body = response.json()
    assert body["asset_query"] == "ETH"
    assert len(body["asset_candidates"]) == 1
    assert body["asset_candidates"][0]["symbol"] == "ETH"


async def test_draft_skips_search_when_asset_query_missing(
    client_factory: Any,
    auth_headers: dict[str, str],
) -> None:
    """LLM didn't surface an asset → no CoinGecko call → empty
    candidates → bot opens the asset picker."""
    drafter = _FakeDrafter(
        response=_draft_with(asset_query=None, thesis="something vague"),
    )
    market = _FakeMarketData()
    client = await client_factory(drafter, market)
    response = await client.post(
        "/claims/draft",
        json={"text": "something vague"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert market.search_calls == [], (
        "When the drafter returns asset_query=None the route MUST NOT "
        "invent a search query — the bot's missing-field flow takes over."
    )
    body = response.json()
    assert body["asset_query"] is None
    assert body["asset_candidates"] == []


async def test_draft_503_when_drafter_fails(
    client_factory: Any,
    auth_headers: dict[str, str],
) -> None:
    """LLM transport failure → 503 → bot falls back to the form."""
    drafter = _FakeDrafter(
        response=_draft_with(asset_query=None, thesis="..."),
        raise_with=ClaimDrafterError("openrouter exploded"),
    )
    market = _FakeMarketData()
    client = await client_factory(drafter, market)
    response = await client.post(
        "/claims/draft",
        json={"text": "anything"},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert market.search_calls == []


async def test_draft_coingecko_failure_is_non_fatal(
    client_factory: Any,
    auth_headers: dict[str, str],
) -> None:
    """CoinGecko being down doesn't fail the draft — the bot just shows
    the asset picker instead of pre-filled candidates. The LLM-extracted
    fields (direction/horizon/conviction/asset_query) still surface."""
    drafter = _FakeDrafter(
        response=_draft_with(asset_query="SOL", thesis="SOL pumping tomorrow"),
    )
    market = _FakeMarketData(raise_with=MarketDataError("coingecko 502"))
    client = await client_factory(drafter, market)
    response = await client.post(
        "/claims/draft",
        json={"text": "SOL pumping tomorrow"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["asset_query"] == "SOL"
    assert body["asset_candidates"] == []
    assert body["direction"] == "long"


# ---------------------------------------------------------------------------
# POST /claims — persist + enqueue
# ---------------------------------------------------------------------------


def _submit_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal valid ``SubmitClaimRequest`` body.

    Includes ``parse_metadata.extracted_at`` — the LLM path always
    populates it, and the JSONB serializer must not choke on it (the
    Sprint-1 live bug: the route serialized the dict via the default
    ``json.dumps`` which can't handle datetimes, surfacing as a 500
    on every real LLM-driven /alpha Confirm).
    """
    base: dict[str, Any] = {
        "coingecko_id": "ethereum",
        "direction": "long",
        "horizon": "14d",
        "conviction": "strong",
        "thesis_text": "ETH long 2 weeks, Vitalik staking news",
        "tg_user_id": 42,
        "asset_query": "ETH",
        "extracted_fields": {"schema_version": 1},
        "parse_metadata": {
            "parsed_via": "freeform_llm",
            "model": "openai/gpt-5-mini",
            "raw_user_turns": ["ETH long 2 weeks"],
            "confidences": {"asset_query": 0.95},
            "extracted_at": "2026-05-29T21:38:18.367556Z",
        },
    }
    base.update(overrides)
    return base


def _eth_metadata() -> AssetMetadata:
    return AssetMetadata(
        coingecko_id="ethereum",
        symbol="ETH",
        name="Ethereum",
    )


async def test_submit_claim_persists_before_enqueueing(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Sprint-1 latent bug: the route enqueued an RQ job but never
    INSERTed the row. This test pins the contract that persistence
    happens BEFORE the enqueue — by the time the worker fires, the
    claim row exists."""
    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    market = _FakeMarketData(metadata_response=_eth_metadata())
    queue = _RecordingQueue()
    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:0000111122223333abcdabcd00001111"
    session.claims.queue_observer = queue

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/claims",
            json=_submit_payload(),
            headers=auth_headers,
        )

    assert response.status_code == 201, response.text
    assert len(session.claims.created) == 1, "exactly one claim row should be created"
    assert session.committed, "route must commit after both persist + enqueue"

    # The persist-before-enqueue contract:
    assert session.claims.create_calls_before_enqueue == [True], (
        "the route MUST persist the claim row BEFORE enqueueing the "
        "resolution job — otherwise the worker fires on a phantom row "
        "if the INSERT fails"
    )
    assert len(queue.enqueues) == 1, "one RQ enqueue per submit"

    # The created row carries the fields the bot sent (including the
    # tg_user_id-resolved source_id + the LLM audit fields).
    kwargs = session.claims.created[0]
    assert kwargs["source_id"] == "tg:0000111122223333abcdabcd00001111"
    assert kwargs["asset_coingecko_id"] == "ethereum"
    assert kwargs["asset_symbol"] == "ETH"
    assert kwargs["direction"] == "long"
    assert kwargs["horizon"] == "14d"
    assert kwargs["conviction"] == "strong"
    assert kwargs["asset_query"] == "ETH"
    assert kwargs["parsed_via"] == "freeform_llm"
    # The persisted row's claim_id matches what the route returned.
    assert UUID(response.json()["claim_id"]) == kwargs["claim_id"]


async def test_submit_claim_unknown_user_404s(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-onboarded user must NOT be able to write a claim — that
    would violate the FK on ``claims.source_id`` and surface a 500.
    Better to 404 the un-onboarded caller and point them at /start.
    Also: the route must NOT enqueue or commit on the 404 path."""
    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    market = _FakeMarketData(metadata_response=_eth_metadata())
    queue = _RecordingQueue()
    session = _FakeSession()
    # No entry in identities → unknown user.

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/claims",
            json=_submit_payload(tg_user_id=9999),
            headers=auth_headers,
        )

    assert response.status_code == 404
    assert session.claims.created == []
    assert queue.enqueues == []
    assert not session.committed


async def test_submit_claim_jsonb_columns_are_json_serializable(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``parse_metadata`` carries an ``extracted_at``
    datetime from the LLM. The route MUST serialize the JSONB columns
    via ``model_dump(mode='json')`` so they round-trip through
    ``json.dumps`` — otherwise SQLAlchemy's default JSONB serializer
    raises "Object of type datetime is not JSON serializable" at
    flush time and every real LLM-driven /alpha Confirm 500s.
    """
    import json

    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    market = _FakeMarketData(metadata_response=_eth_metadata())
    queue = _RecordingQueue()
    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:" + "a" * 32

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/claims", json=_submit_payload(), headers=auth_headers)
    assert response.status_code == 201, response.text

    # The values the route hands to the repo must be plain JSON —
    # ``json.dumps`` over them must NOT raise. This is the byte-level
    # guarantee that the JSONB column will accept them.
    kwargs = session.claims.created[0]
    try:
        json.dumps(kwargs["extracted_fields"])
        json.dumps(kwargs["parse_metadata"])
    except TypeError as e:  # pragma: no cover — failure path
        pytest.fail(
            f"JSONB column payload is not JSON-serializable: {e}. "
            "The route must call model_dump(mode='json') on the "
            "ExtractedFieldsDTO / ParseMetadataDTO so datetimes "
            "(notably parse_metadata.extracted_at) round-trip cleanly."
        )
    # And the datetime field specifically should now be a string
    # (ISO-format), not a datetime object.
    assert isinstance(kwargs["parse_metadata"]["extracted_at"], str), (
        "parse_metadata.extracted_at must be serialized to a string "
        "before reaching the JSONB column."
    )


async def test_submit_claim_captures_entry_price_snapshot(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit must persist + return the price-at-submit snapshot.

    User-visible promise: receipt + /me show the price the user was
    betting against. The API takes one CoinGecko ``get_price_at``
    reading at ``submitted_at`` and threads the result down to the
    repository (NULLABLE column) and back up to ``ClaimResponse``.
    """
    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    submit_ts_capture: list[datetime] = []
    market = _FakeMarketData(
        metadata_response=_eth_metadata(),
        price_response=PricePoint(
            coingecko_id="ethereum",
            requested_at=datetime.now(tz=UTC),
            returned_at=datetime.now(tz=UTC),
            price_usd=2438.12,
        ),
    )
    queue = _RecordingQueue()
    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:" + "a" * 32

    # Sanity-check that the route asks for the price at submitted_at
    # (not some other timestamp). We capture what the fake saw.
    real_get_price_at = market.get_price_at

    async def _capturing_get_price_at(coingecko_id: str, when: datetime) -> PricePoint:
        submit_ts_capture.append(when)
        return await real_get_price_at(coingecko_id, when)

    market.get_price_at = _capturing_get_price_at  # type: ignore[method-assign]

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/claims", json=_submit_payload(), headers=auth_headers)

    assert response.status_code == 201, response.text
    # Persisted on the row.
    kwargs = session.claims.created[0]
    assert kwargs["price_usd_at_submit"] == pytest.approx(2438.12)
    # Echoed back to the bot in the response so the receipt can
    # render the entry-price line without a second round-trip.
    assert response.json()["price_usd_at_submit"] == pytest.approx(2438.12)
    # The snapshot was taken at submitted_at (within the same
    # request — the route doesn't reuse a stale timestamp).
    assert len(submit_ts_capture) == 1
    assert submit_ts_capture[0] == kwargs["submitted_at"]


async def test_submit_claim_market_data_failure_does_not_block_submit(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoinGecko being down at submit must NOT fail the bet.

    Same UX-acceleration-not-system-dependency rule as the LLM
    fallback: market data is best-effort. The route persists
    ``price_usd_at_submit=NULL`` and returns 201 with the field set
    to ``None`` so the bot's receipt renderer omits the price line
    (never invents a placeholder, never shows ``$0.00``).
    """
    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    market = _FakeMarketData(
        metadata_response=_eth_metadata(),
        price_raise_with=MarketDataError("coingecko down at submit"),
    )
    queue = _RecordingQueue()
    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:" + "a" * 32

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/claims", json=_submit_payload(), headers=auth_headers)

    # Submit still succeeded.
    assert response.status_code == 201, response.text
    # But the price column is NULL — both on the persisted row and
    # in the wire response.
    assert session.claims.created[0]["price_usd_at_submit"] is None
    assert response.json()["price_usd_at_submit"] is None
    # And the bet still got enqueued — market-data failure must not
    # leave the row stranded.
    assert len(queue.enqueues) == 1


async def test_submit_claim_rq_job_id_uses_dash_separator(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint-1 regression: RQ rejects colons in job_ids. The enqueue
    must use ``resolve-{uuid}`` (dash), not ``resolve:{uuid}``."""
    drafter = _FakeDrafter(response=_draft_with(asset_query="ETH", thesis="..."))
    market = _FakeMarketData(metadata_response=_eth_metadata())
    queue = _RecordingQueue()
    session = _FakeSession()
    session.identities.by_tg_id[42] = "tg:" + "a" * 32

    app = _build_app(
        drafter=drafter,
        market=market,
        queue=queue,
        session=session,
        monkeypatch=monkeypatch,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/claims", json=_submit_payload(), headers=auth_headers)
    assert response.status_code == 201
    _when, _fn, _args, kwargs = queue.enqueues[0]
    job_id = kwargs.get("job_id", "")
    assert job_id.startswith("resolve-"), f"expected dash separator, got {job_id!r}"
    assert ":" not in job_id, "RQ rejects colons — the historical bug"
