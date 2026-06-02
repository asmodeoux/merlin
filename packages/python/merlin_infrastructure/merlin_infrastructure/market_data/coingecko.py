"""``CoinGeckoAdapter`` — concrete ``MarketDataPort`` (Q-026 single source).

httpx-backed wrapper over the public ``api.coingecko.com/api/v3``
surface. Uses the Demo tier on Sprint 1 (free key) but the env var is
named ``COINGECKO_API_KEY`` so the same name carries forward to paid
tiers — only the base URL discriminates the tier.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from merlin_core.application.ports import (
    AssetMetadata,
    AssetNotFoundError,
    MarketDataError,
    PricePoint,
)
from merlin_core.domain import AssetCandidate

_log = logging.getLogger("merlin.infrastructure.market_data.coingecko")

_DEFAULT_TIMEOUT_S: Final = 15.0
# Q-026: accept ±5-minute timestamp rounding from CoinGecko's bucketed
# ranges. The resolver passes the requested ``when``; we ask CoinGecko
# for a small window around it and pick the closest bucket.
_PRICE_WINDOW = timedelta(minutes=5)
_MAX_SEARCH_RESULTS = 5


class CoinGeckoAdapter:
    """Concrete ``MarketDataPort`` against CoinGecko's REST API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.coingecko.com/api/v3",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure and hold an httpx client; key may be None for public anonymous calls."""
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = http_client or httpx.AsyncClient(timeout=timeout_s)

    def _check_status(
        self,
        response: httpx.Response,
        endpoint: str,
        *,
        treat_404_as_not_found: bool = False,
        not_found_id: str | None = None,
    ) -> None:
        """Map a CoinGecko HTTP status onto a domain error.

        Defends against two failure modes that have already cost us live
        debugging time:

        * **3xx redirect** — only happens when ``COINGECKO_BASE_URL`` is
          misconfigured (e.g. missing the ``/api/v3`` suffix, which makes
          the public site issue a 301 to the marketing route). Without
          this guard, ``response.json()`` further down crashes on an empty
          HTML body and the resolver retries to exhaustion with no useful
          log.
        * **4xx / 5xx** — surface status + a body preview so on-call has
          something to grep.

        ``treat_404_as_not_found`` flips the 404 case to
        ``AssetNotFoundError`` for endpoints where 404 carries that exact
        meaning (``/coins/{id}`` family).
        """
        status = response.status_code
        if treat_404_as_not_found and status == 404:
            raise AssetNotFoundError(not_found_id or endpoint)
        if 300 <= status < 400:
            raise MarketDataError(
                f"CoinGecko {endpoint} unexpected redirect {status} "
                f"(Location={response.headers.get('location')!r}). "
                f"Likely COINGECKO_BASE_URL is missing the '/api/v3' suffix."
            )
        if status >= 400:
            raise MarketDataError(f"CoinGecko {endpoint} {status}: {response.text[:200]}")

    @staticmethod
    def _parse_json(response: httpx.Response, endpoint: str) -> Any:
        """Decode the body as JSON or raise ``MarketDataError`` with context.

        A 2xx response with a non-JSON body (rare — happens on certain
        proxy / WAF responses) used to crash the resolver with a bare
        ``JSONDecodeError`` and no body preview. Surfacing the status +
        body prefix keeps the failure debuggable.
        """
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise MarketDataError(
                f"CoinGecko {endpoint} returned non-JSON body "
                f"(status={response.status_code}, "
                f"content-type={response.headers.get('content-type')!r}): "
                f"{response.text[:200]!r}"
            ) from e

    def _headers(self) -> dict[str, str]:
        """Return request headers, including the API key when configured."""
        if self._api_key:
            # Demo tier uses x-cg-demo-api-key; Pro tier uses x-cg-pro-api-key.
            # The base URL discriminates which header CoinGecko expects.
            header_name = (
                "x-cg-pro-api-key"
                if "pro-api.coingecko.com" in self._base_url
                else "x-cg-demo-api-key"
            )
            return {header_name: self._api_key}
        return {}

    async def search_assets(self, query: str) -> list[AssetCandidate]:
        """``GET /search?query=...`` → top-5 ``AssetCandidate`` ordered by market-cap rank."""
        try:
            response = await self._client.get(
                f"{self._base_url}/search",
                params={"query": query},
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            raise MarketDataError(f"CoinGecko transport error: {e}") from e
        self._check_status(response, "/search")
        payload = self._parse_json(response, "/search")
        coins = payload.get("coins", [])
        return [
            AssetCandidate(
                coingecko_id=str(c["id"]),
                symbol=str(c["symbol"]).upper(),
                name=str(c["name"]),
                market_cap_rank=c.get("market_cap_rank"),
            )
            for c in coins[:_MAX_SEARCH_RESULTS]
        ]

    async def get_asset_metadata(self, coingecko_id: str) -> AssetMetadata:
        """``GET /coins/{id}`` → canonical ``(symbol, name)``."""
        try:
            response = await self._client.get(
                f"{self._base_url}/coins/{coingecko_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "false",
                    "community_data": "false",
                    "developer_data": "false",
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            raise MarketDataError(f"CoinGecko transport error: {e}") from e
        self._check_status(
            response, "/coins", treat_404_as_not_found=True, not_found_id=coingecko_id
        )
        payload = self._parse_json(response, "/coins")
        return AssetMetadata(
            coingecko_id=coingecko_id,
            symbol=str(payload["symbol"]).upper(),
            name=str(payload["name"]),
        )

    async def get_price_at(self, coingecko_id: str, when: datetime) -> PricePoint:
        """``GET /coins/{id}/market_chart/range`` → nearest price point ±5min."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        start = when - _PRICE_WINDOW
        end = when + _PRICE_WINDOW
        try:
            response = await self._client.get(
                f"{self._base_url}/coins/{coingecko_id}/market_chart/range",
                params={
                    "vs_currency": "usd",
                    "from": int(start.timestamp()),
                    "to": int(end.timestamp()),
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            raise MarketDataError(f"CoinGecko transport error: {e}") from e
        self._check_status(
            response,
            "/market_chart/range",
            treat_404_as_not_found=True,
            not_found_id=coingecko_id,
        )
        payload = self._parse_json(response, "/market_chart/range")
        prices = payload.get("prices", [])
        if not prices:
            raise AssetNotFoundError(f"empty price series for {coingecko_id}")

        target_ms = int(when.timestamp() * 1000)
        ts_ms, price = min(prices, key=lambda p: abs(p[0] - target_ms))
        returned_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        return PricePoint(
            coingecko_id=coingecko_id,
            requested_at=when,
            returned_at=returned_at,
            price_usd=float(price),
        )
