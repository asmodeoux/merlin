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

from datetime import UTC, datetime, timedelta

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
async def test_get_price_at_empty_narrow_then_empty_wide_raises_market_data_error() -> None:
    """Empty narrow + empty wide → ``MarketDataError`` (NOT asset-not-found).

    Live regression: an empty ``prices`` payload from CoinGecko used to
    raise ``AssetNotFoundError`` (read by the resolver as a Q-023 asset
    mutation), so a transient Demo-tier sparsity hiccup permanently
    rejected a user's bet with the misleading "your token's listing
    changed" notification.

    The fix's contract: the adapter retries once with a wider window
    (±30min) before declaring the asset missing, and even on a second
    empty payload it raises ``MarketDataError`` so RQ's retry policy
    can pick the job up again. ``AssetNotFoundError`` is now reserved
    for the genuine 404-from-CoinGecko path.
    """
    route = respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, json={"prices": []})
    )
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    with pytest.raises(MarketDataError) as exc:
        await _adapter().get_price_at("bitcoin", when)
    msg = str(exc.value)
    assert "no prices" in msg
    assert "bitcoin" in msg
    assert "RQ will retry" in msg
    # Two calls: the canonical ±5min lookup + the ±30min fallback. The
    # adapter must NOT widen indefinitely — exactly one retry, then
    # surface up to the resolver / RQ.
    assert route.call_count == 2


@respx.mock
async def test_get_price_at_empty_narrow_then_populated_wide_recovers() -> None:
    """Empty ±5min + populated ±30min → return the closest bucket (recovery path).

    Pins the SOL-bet recovery scenario: CoinGecko's narrow lookup
    returns no buckets (sparsity around the exact minute) but a wider
    lookup catches the nearest one. The adapter returns the closest
    available price rather than rejecting the bet.
    """
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    narrow_from = int((when - timedelta(minutes=5)).timestamp())
    wide_from = int((when - timedelta(minutes=30)).timestamp())
    # The narrow window returns empty; the wide window catches a bucket
    # ~12min before ``when`` (within ±30min, outside ±5min).
    respx.get(f"{_BASE_URL}/coins/solana/market_chart/range", params={"from": narrow_from}).mock(
        return_value=httpx.Response(200, json={"prices": []})
    )
    respx.get(f"{_BASE_URL}/coins/solana/market_chart/range", params={"from": wide_from}).mock(
        return_value=httpx.Response(
            200,
            json={"prices": [[target_ms - 720_000, 142.5], [target_ms - 1_500_000, 140.0]]},
        )
    )
    pp = await _adapter().get_price_at("solana", when)
    # Closest bucket (-12min) wins, not the further one (-25min).
    assert pp.price_usd == pytest.approx(142.5)
    assert pp.coingecko_id == "solana"


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
