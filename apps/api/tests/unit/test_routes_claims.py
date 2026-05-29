"""Unit tests for ``POST /claims/draft`` — the freeform-LLM extraction
endpoint.

Sprint-1 live bug: the route was searching CoinGecko with ``thesis_text``
(the full sentence) instead of the LLM-extracted ``asset_query`` (a
short symbol). Obvious calls like *"ETH long 2 weeks, Vitalik staking
news"* landed with zero asset candidates. These tests pin the wiring
contract:

* The asset search runs against ``draft.asset_query``, NOT
  ``draft.thesis_text``.
* When the LLM didn't surface an asset (``draft.asset_query is None``),
  the API does not call ``search_assets`` at all — the bot opens the
  picker instead.
* LLM transport errors map to 503 and the bot falls back to the form.

These are wiring tests, not LLM/CoinGecko integration tests. Both ports
are replaced with hand-rolled fakes.
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
from merlin_api.deps import Ports, get_ports, get_settings
from merlin_core.application.ports import ClaimDrafterError, MarketDataError
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
    """Records every ``search_assets`` query so the test can pin the contract."""

    candidates: list[AssetCandidate] = field(default_factory=list)
    search_calls: list[str] = field(default_factory=list)
    raise_with: Exception | None = None

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        self.search_calls.append(query)
        if self.raise_with is not None:
            raise self.raise_with
        return self.candidates

    async def get_asset_metadata(self, *_a: Any, **_kw: Any) -> None:  # unused here
        raise NotImplementedError


class _NoopQueue:
    def enqueue_at(self, *_a: Any, **_kw: Any) -> None:
        pass


class _NoopNotifier:
    async def notify(self, *_a: Any, **_kw: Any) -> None:
        pass


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


def _build_app(*, drafter: _FakeDrafter, market: _FakeMarketData) -> Any:
    """Build a FastAPI app with the given drafter + market data."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )
    app.dependency_overrides[get_ports] = lambda: Ports(
        drafter=drafter,
        market_data=market,
        queue=_NoopQueue(),
        notifier=_NoopNotifier(),
    )
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
