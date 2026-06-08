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
# Fallback window used ONLY when the narrow ±5min lookup returns an
# empty ``prices`` series. Lets us tell apart "CoinGecko was sparse
# around this exact minute" (recoverable — widen + try again) from
# "the asset genuinely no longer exists" (HTTP 404 — Q-023 reject).
#
# Live regression that motivated this: a SOL bet got the
# "token's listing changed during your window" Q-023 rejection at
# resolve time even though ``coingecko_id`` is the rock-stable
# ``"solana"`` (CoinGecko has never renamed it). Root cause: the
# Demo tier returned ``200 OK`` with ``{"prices": []}`` for the
# ±5min slice — sparsity / silent rate-limit pushback, NOT a
# delisting — and the adapter mapped that to ``AssetNotFoundError``.
# Widening to ±30min before declaring asset-gone catches the sparse
# case (CoinGecko's free tier guarantees 5-min granularity inside
# the last 24h; ±30min always straddles enough buckets) while
# staying narrow enough to keep the picked price meaningful for
# short-horizon bets.
_PRICE_WINDOW_WIDE = timedelta(minutes=30)
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
        """``GET /coins/{id}/market_chart/range`` → nearest price point ±5min.

        Two-stage lookup so an empty CoinGecko response can't masquerade
        as a missing asset:

        * **Stage 1** (canonical, Q-026): ±5min around ``when``. Almost
          every call resolves here — CoinGecko's free tier guarantees
          5-min granularity within the last 24h.
        * **Stage 2** (fallback): only if stage 1 returns ``200 OK`` with
          ``prices: []``. Widen to ±30min and try once more. Closes the
          live SOL-bet bug where the Demo tier returned an empty payload
          for the narrow window (sparsity / silent rate-limit pushback)
          and the resolver permanently rejected the claim as a Q-023
          asset mutation.

        After both stages, if there are still no prices we raise
        ``MarketDataError`` — *not* ``AssetNotFoundError``. The
        resolver propagates ``MarketDataError`` upward so RQ's
        ``Retry(max=3, interval=[60, 300, 900])`` policy kicks in
        (1m / 5m / 15m). After exhausting retries the user gets the
        honest ``resolution_failed_final`` template ("market data is
        taking longer than usual") instead of the misleading
        "the token's listing changed" rejection. Genuine 404 from
        CoinGecko still raises ``AssetNotFoundError`` (handled in
        :meth:`_fetch_market_chart_range`) so a real delisting still
        reaches the Q-023 reject path.
        """
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)

        prices = await self._fetch_market_chart_range(
            coingecko_id=coingecko_id,
            when=when,
            window=_PRICE_WINDOW,
        )
        if not prices:
            _log.warning(
                "CoinGecko returned no prices for %s in ±%dmin around %s; "
                "retrying with ±%dmin fallback window before declaring "
                "the asset missing",
                coingecko_id,
                int(_PRICE_WINDOW.total_seconds() / 60),
                when.isoformat(),
                int(_PRICE_WINDOW_WIDE.total_seconds() / 60),
            )
            prices = await self._fetch_market_chart_range(
                coingecko_id=coingecko_id,
                when=when,
                window=_PRICE_WINDOW_WIDE,
            )
        if not prices:
            raise MarketDataError(
                f"CoinGecko returned no prices for {coingecko_id} in "
                f"±{int(_PRICE_WINDOW_WIDE.total_seconds() / 60)}min "
                f"around {when.isoformat()} (narrow ±"
                f"{int(_PRICE_WINDOW.total_seconds() / 60)}min also "
                "empty). Treating as a transient sparsity / rate-limit "
                "hiccup — RQ will retry the resolution job."
            )

        target_ms = int(when.timestamp() * 1000)
        ts_ms, price = min(prices, key=lambda p: abs(p[0] - target_ms))
        returned_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        return PricePoint(
            coingecko_id=coingecko_id,
            requested_at=when,
            returned_at=returned_at,
            price_usd=float(price),
        )

    async def _fetch_market_chart_range(
        self,
        *,
        coingecko_id: str,
        when: datetime,
        window: timedelta,
    ) -> list[list[float]]:
        """One ``/market_chart/range`` GET centred on ``when`` with ``window`` either side.

        Returns the raw ``prices`` list (possibly empty) so the caller
        can decide whether an empty payload is recoverable (widen and
        retry) or terminal (raise ``MarketDataError``).

        Maps the two CoinGecko failure modes that have domain meaning:

        * **HTTP 404** → ``AssetNotFoundError(coingecko_id)``. Genuine
          delisting / id rename. The resolver's Q-023 reject path picks
          this up.
        * **HTTP 3xx / 4xx / 5xx / non-JSON body** → ``MarketDataError``
          via the shared ``_check_status`` + ``_parse_json`` helpers.
        """
        start = when - window
        end = when + window
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
        return list(payload.get("prices", []))
