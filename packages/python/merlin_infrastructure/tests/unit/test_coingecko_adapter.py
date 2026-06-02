"""Unit tests for ``CoinGeckoAdapter`` hardening (Sprint-1 outage fix).

Pinning the failure modes that escaped CI on Sprint-1 and stalled the
resolver retry loop:

* **3xx redirect** — happens when ``COINGECKO_BASE_URL`` is misconfigured
  (missing the ``/api/v3`` suffix). The httpx client doesn't follow
  redirects by default, so the response sails through any
  ``status_code >= 400`` guard and crashes on ``response.json()`` with
  an empty body. Adapter now raises ``MarketDataError`` with a hint
  pointing at the env var.
* **2xx non-JSON body** — rare (proxy / WAF responses) but the bare
  ``JSONDecodeError`` traceback was useless on-call. Adapter now wraps
  it in a ``MarketDataError`` carrying status + body preview.
* **4xx / 5xx** — already mapped, but exercised here to lock the
  message shape that on-call greps for.

Tests use ``respx`` (project dev dep) to mock httpx calls — no real
network. Each test pins one endpoint x one failure mode so a regression
points at exactly which guard slipped.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from merlin_core.application.ports import AssetNotFoundError, MarketDataError
from merlin_infrastructure.market_data.coingecko import CoinGeckoAdapter

_BASE_URL = "https://api.coingecko.com/api/v3"


def _adapter() -> CoinGeckoAdapter:
    """Build an adapter using a fresh client per test for isolation."""
    return CoinGeckoAdapter(base_url=_BASE_URL, http_client=httpx.AsyncClient(timeout=5.0))


@respx.mock
async def test_search_assets_success_returns_top_candidates() -> None:
    """Happy path locks the parse logic + market-cap ordering."""
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                ]
            },
        )
    )
    out = await _adapter().search_assets("btc")
    assert [c.coingecko_id for c in out] == ["bitcoin", "ethereum"]
    assert [c.symbol for c in out] == ["BTC", "ETH"]


@respx.mock
async def test_search_assets_redirect_raises_market_data_error_with_env_hint() -> None:
    """A 301 is always a config bug — never silently fall through to JSON parse.

    This is the exact failure that produced the Sprint-1 outcome
    notification outage: ``COINGECKO_BASE_URL=https://api.coingecko.com``
    (no ``/api/v3``) → 301 to the marketing site → empty body →
    ``JSONDecodeError`` on resolve.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.invalid/en/"})
    )
    with pytest.raises(MarketDataError) as exc:
        await _adapter().search_assets("btc")
    msg = str(exc.value)
    assert "301" in msg
    assert "COINGECKO_BASE_URL" in msg
    assert "/api/v3" in msg


@respx.mock
async def test_search_assets_non_json_2xx_raises_with_body_preview() -> None:
    """A 200 with a non-JSON body must surface the body preview, not crash bare."""
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, text="<html>Just a moment...</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(MarketDataError) as exc:
        await _adapter().search_assets("btc")
    msg = str(exc.value)
    assert "non-JSON" in msg
    assert "200" in msg
    assert "<html>" in msg


@respx.mock
async def test_search_assets_5xx_raises_market_data_error() -> None:
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(503, text="upstream down"))
    with pytest.raises(MarketDataError) as exc:
        await _adapter().search_assets("btc")
    assert "503" in str(exc.value)
    assert "upstream down" in str(exc.value)


@respx.mock
async def test_get_asset_metadata_404_raises_asset_not_found() -> None:
    """``/coins/{id}`` 404 means the asset is genuinely missing — separate type."""
    respx.get(f"{_BASE_URL}/coins/nonexistent").mock(
        return_value=httpx.Response(404, json={"error": "Not Found"})
    )
    with pytest.raises(AssetNotFoundError):
        await _adapter().get_asset_metadata("nonexistent")


@respx.mock
async def test_get_asset_metadata_redirect_raises_market_data_error() -> None:
    respx.get(f"{_BASE_URL}/coins/bitcoin").mock(return_value=httpx.Response(302))
    with pytest.raises(MarketDataError) as exc:
        await _adapter().get_asset_metadata("bitcoin")
    assert "302" in str(exc.value)
    assert "COINGECKO_BASE_URL" in str(exc.value)


@respx.mock
async def test_get_price_at_redirect_raises_market_data_error() -> None:
    """The exact failure path that broke the resolver — pin it here forever."""
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(301, headers={"location": "https://api.coingecko.com/en/"})
    )
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    with pytest.raises(MarketDataError) as exc:
        await _adapter().get_price_at("bitcoin", when)
    msg = str(exc.value)
    assert "301" in msg
    assert "COINGECKO_BASE_URL" in msg


@respx.mock
async def test_get_price_at_404_raises_asset_not_found() -> None:
    respx.get(f"{_BASE_URL}/coins/nope/market_chart/range").mock(return_value=httpx.Response(404))
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    with pytest.raises(AssetNotFoundError):
        await _adapter().get_price_at("nope", when)


@respx.mock
async def test_get_price_at_non_json_2xx_raises_with_body_preview() -> None:
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text="", headers={"content-type": "text/html"})
    )
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    with pytest.raises(MarketDataError) as exc:
        await _adapter().get_price_at("bitcoin", when)
    assert "non-JSON" in str(exc.value)


@respx.mock
async def test_get_price_at_empty_prices_raises_asset_not_found() -> None:
    """A 2xx + valid JSON but no price points is also an asset-availability problem."""
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, json={"prices": []})
    )
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    with pytest.raises(AssetNotFoundError):
        await _adapter().get_price_at("bitcoin", when)


@respx.mock
async def test_get_price_at_picks_nearest_bucket() -> None:
    """CoinGecko returns 5-min buckets; adapter picks the closest to ``when``."""
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            json={
                "prices": [
                    [target_ms - 240_000, 100.0],
                    [target_ms - 30_000, 101.5],
                    [target_ms + 270_000, 102.0],
                ]
            },
        )
    )
    pp = await _adapter().get_price_at("bitcoin", when)
    assert pp.price_usd == pytest.approx(101.5)
