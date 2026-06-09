"""``CoinGeckoAdapter`` — concrete ``MarketDataPort`` (Q-026 single source).

httpx-backed wrapper over the public ``api.coingecko.com/api/v3``
surface. Uses the Demo tier on Sprint 1 (free key) but the env var is
named ``COINGECKO_API_KEY`` so the same name carries forward to paid
tiers — only the base URL discriminates the tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
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
# Q-026: ±5-minute price window around the requested ``when``. The
# resolver passes a target timestamp; we ask CoinGecko for the bucket
# series across this window and TWAP across it. Window width is the
# institutional sweet spot — wide enough to smooth single-bucket noise
# (a 5-min spike where the user's ``t_resolve`` happened to land), narrow
# enough that the picked price remains meaningful for short-horizon
# bets.
_PRICE_WINDOW = timedelta(minutes=5)
# Fallback window used ONLY when the canonical ±5min lookup returns an
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
# staying narrow enough to keep the TWAP meaningful for short-horizon
# bets.
_PRICE_WINDOW_WIDE = timedelta(minutes=30)
_MAX_SEARCH_RESULTS = 5

# EVM contract-address shape — strict 40 hex chars after ``0x``. Used
# only as a *substring* match so a paste like
# ``https://etherscan.io/token/0xa0b8…48`` still extracts the address.
# Case-insensitive on the hex digits because EVM addresses are
# routinely shown in EIP-55 mixed case.
_EVM_ADDRESS_RE: Final = re.compile(r"0x[a-fA-F0-9]{40}")

# Fan-out platform list for the contract-address fallback path. These
# are the five EVM platforms with the highest CoinGecko-indexed market
# share — empirically catching ~90% of contract-address pastes a user
# is plausibly going to send. Each entry is the CoinGecko-canonical
# ``platform_id`` (matches ``/coins/{platform}/contract/{address}``);
# extending the list is a one-line tuple edit.
_CONTRACT_LOOKUP_PLATFORMS: Final = (
    "ethereum",
    "polygon-pos",
    "base",
    "arbitrum-one",
    "binance-smart-chain",
)


def _extract_evm_address(query: str) -> str | None:
    """Return the lowercased EVM address embedded in ``query`` or ``None``.

    Matches anywhere in the string so an explorer URL like
    ``https://etherscan.io/token/0xabc…``, a "look at this token
    ``0xabc…``" prose comment, or the bare 42-char address all
    surface the same hex string. The returned address is lowercased
    so downstream lookups don't pay the EIP-55 mixed-case lottery.

    Returns ``None`` for plain tickers / coin names / homepage URLs
    — anything that doesn't look like an EVM contract is left to the
    existing fuzzy / exact pipeline.
    """
    match = _EVM_ADDRESS_RE.search(query)
    return match.group(0).lower() if match else None


def _safe_float(value: Any) -> float | None:
    """Coerce a CoinGecko-supplied numeric field to ``float | None``.

    The ``/coins/markets`` endpoint legitimately returns ``null`` for
    ``current_price`` / ``market_cap`` on long-tail rows (e.g. coins
    that have stopped trading recently). Anything that can't be
    coerced — ``None``, strings, etc. — falls through as ``None`` so
    the picker renders "—" rather than crashing.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_like_ticker(query: str) -> bool:
    """Decide whether to issue the exact-symbol probe alongside ``/search``.

    Tickers are never multi-word, so a query containing whitespace
    ("bitcoin cash", "ethereum classic") is a name and the exact-
    symbol probe is guaranteed to return empty. Skipping it for those
    saves a CoinGecko call on the Demo tier with zero loss of
    coverage. Single-word queries always probe — including names
    like "ethereum" — because the cost of one wasted call is far
    smaller than the cost of missing a legitimate low-cap exact-
    ticker hit (this is the bug that motivated the second endpoint
    in the first place).
    """
    q = query.strip()
    return bool(q) and not any(ch.isspace() for ch in q)


def _prioritise_exact_ticker(candidates: list[AssetCandidate], query: str) -> list[AssetCandidate]:
    """Float exact-ticker matches to the top, sorted by market-cap rank.

    Stable on non-matching candidates: their CoinGecko-native order is
    preserved. Among the exact matches, candidates with a known
    ``market_cap_rank`` come first (ascending — rank 1 is the most
    legitimate); candidates with ``None`` rank are kept after, in
    their original relative order. This handles the "many low-cap
    scam tokens share a popular ticker" case by surfacing the most
    reputable exact match first without flooding the picker.

    Defensive belt-and-suspenders on top of the ``/coins/markets``
    exact-symbol probe in :meth:`search_assets`: if that probe is
    unavailable but an exact match happens to surface in the fuzzy
    ``/search`` results, this helper still floats it to the top.
    """
    if not candidates:
        return candidates
    query_upper = query.strip().upper()
    if not query_upper:
        return candidates
    exact = [c for c in candidates if c.symbol == query_upper]
    other = [c for c in candidates if c.symbol != query_upper]
    exact.sort(key=lambda c: (c.market_cap_rank is None, c.market_cap_rank or 0))
    return exact + other


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
        """Fan out to ``/search`` + ``/coins/markets?symbols=`` and merge → top-5.

        Dispatches on input shape before doing any work:

        * **EVM contract address** (matches ``0x[a-fA-F0-9]{40}``
          anywhere in the input — bare or embedded in an explorer URL)
          → hands off to :meth:`_search_by_contract` which tries
          ``/search`` first and falls back to a per-chain
          ``/coins/{platform}/contract/{address}`` fan-out.
        * **Everything else** (ticker, coin name) → the original
          two-endpoint pipeline below.

        Two endpoints because each one alone is wrong:

        * **``/search?query=<q>``** does substring fuzzy matching across
          every coin's name + symbol, ranked by market cap. For 1-2
          letter queries this fans out across the entire 17k+ coin
          universe and a legitimate low-cap exact-symbol coin gets
          buried below the first 25 returned results. Live regression
          motivating the rewrite: querying ``H`` returned
          ``[ETH, USDT, FIGR_HELOC, HYPE, ZEC, ...]`` — every entry
          merely *contains* the letter "h" — and Humanity Protocol
          (``id=humanity``, ``symbol=H``, rank ~161) never surfaced.
        * **``/coins/markets?vs_currency=usd&symbols=<q>&order=market_cap_desc``**
          does exact (case-insensitive) ticker matching against listed
          coins, market-cap ordered. Returns Humanity directly for
          ``symbols=h`` but doesn't do any fuzzy / prefix matching, so
          on its own it'd kill the "type 'bitc' to find Bitcoin"
          discovery affordance.

        Strategy: issue both concurrently. Prepend the exact-symbol
        hits (CoinGecko already orders them market-cap-desc) to the
        fuzzy hits, dedupe by ``coingecko_id`` (a popular ticker like
        ``BTC`` appears in both responses), apply the defensive
        :func:`_prioritise_exact_ticker` pass for the corner case
        where the exact endpoint is unreachable but the fuzzy results
        happened to include the exact match, and truncate to top-5.

        Failure semantics:

        * **Fuzzy ``/search`` failure** surfaces — the picker has
          nothing to show without it.
        * **Exact ``/coins/markets`` failure** is logged and swallowed.
          The fix is a quality-of-life upgrade on the rare short-ticker
          case; a transient outage on the secondary endpoint must not
          knock out the primary picker.

        Queries with whitespace (``"bitcoin cash"``) skip the exact
        probe entirely — no coin has a multi-word ticker so it'd burn
        quota for a guaranteed-empty response.
        """
        address = _extract_evm_address(query)
        if address is not None:
            return await self._search_by_contract(address)
        if _looks_like_ticker(query):
            fuzzy_result, exact_result = await asyncio.gather(
                self._search_fuzzy(query),
                self._search_exact_symbol(query),
                return_exceptions=True,
            )
            if isinstance(fuzzy_result, BaseException):
                raise fuzzy_result
            if isinstance(exact_result, BaseException):
                _log.warning(
                    "CoinGecko exact-symbol lookup for %r failed; "
                    "returning fuzzy-only results. Error: %s",
                    query,
                    exact_result,
                )
                exact: list[AssetCandidate] = []
            else:
                exact = exact_result
            fuzzy: list[AssetCandidate] = fuzzy_result
        else:
            fuzzy = await self._search_fuzzy(query)
            exact = []

        seen_ids = {c.coingecko_id for c in exact}
        merged = exact + [c for c in fuzzy if c.coingecko_id not in seen_ids]
        top = _prioritise_exact_ticker(merged, query)[:_MAX_SEARCH_RESULTS]
        return await self._enrich_with_market_data(top)

    async def _enrich_with_market_data(
        self,
        candidates: list[AssetCandidate],
    ) -> list[AssetCandidate]:
        """Fill ``current_price_usd`` + ``market_cap_usd`` for the picker.

        The exact-symbol path (``/coins/markets?symbols=``) already
        produced enriched ``AssetCandidate`` rows. The fuzzy path
        (``/search``) returns only ``id / symbol / name / rank`` — no
        price or market cap — and that's the data the bot needs to
        differentiate look-alike candidates (multiple distinct coins
        sharing ``(symbol, name)``, e.g. three "STABLE — Stable"
        rows).

        One follow-up call to ``/coins/markets?ids=<id1,id2,...>``
        enriches whatever's still missing. We only request the ids
        we don't already have data for; if the helper raises, we
        log + return the partially-enriched list rather than
        knocking out the picker. The body renderer prints "—" for
        any candidate the helper couldn't fill.
        """
        missing_ids = [
            c.coingecko_id
            for c in candidates
            if c.current_price_usd is None and c.market_cap_usd is None
        ]
        if not missing_ids:
            return candidates
        try:
            enrichments = await self._fetch_markets_by_ids(missing_ids)
        except MarketDataError as e:
            _log.warning(
                "CoinGecko market-data enrichment for %d candidates failed; "
                "rendering picker without price/cap. Error: %s",
                len(missing_ids),
                e,
            )
            return candidates
        return [
            (
                replace(
                    c,
                    current_price_usd=enrichments[c.coingecko_id][0],
                    market_cap_usd=enrichments[c.coingecko_id][1],
                )
                if c.coingecko_id in enrichments
                and c.current_price_usd is None
                and c.market_cap_usd is None
                else c
            )
            for c in candidates
        ]

    async def _fetch_markets_by_ids(
        self,
        ids: list[str],
    ) -> dict[str, tuple[float | None, float | None]]:
        """``GET /coins/markets?ids=<csv>`` → ``{id: (price_usd, market_cap_usd)}``.

        Used by :meth:`_enrich_with_market_data` to fill the missing
        ``current_price_usd`` + ``market_cap_usd`` columns on
        candidates that came from the fuzzy ``/search`` path. Same
        endpoint shape as :meth:`_search_exact_symbol` (just keyed by
        ``ids`` instead of ``symbols``), so the same defensive parse
        rules apply: a 2xx body of an unexpected shape returns an
        empty mapping rather than crashing.
        """
        try:
            response = await self._client.get(
                f"{self._base_url}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ",".join(ids),
                    "order": "market_cap_desc",
                    "per_page": len(ids),
                    "page": 1,
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            raise MarketDataError(f"CoinGecko transport error: {e}") from e
        self._check_status(response, "/coins/markets")
        payload = self._parse_json(response, "/coins/markets")
        if not isinstance(payload, list):
            return {}
        result: dict[str, tuple[float | None, float | None]] = {}
        for entry in payload:
            cg_id = entry.get("id")
            if not isinstance(cg_id, str):
                continue
            result[cg_id] = (
                _safe_float(entry.get("current_price")),
                _safe_float(entry.get("market_cap")),
            )
        return result

    async def _search_by_contract(self, address: str) -> list[AssetCandidate]:
        """Resolve a 40-hex EVM contract address to ``AssetCandidate``s.

        Two-stage strategy with the cheap path tried first:

        * **Stage 1** — ``GET /search?query=<address>``. CoinGecko's
          general search index covers "name, symbol or contract address"
          per the API docs, so a popular token like USDC surfaces here
          in a single hop. Any hit is run through the existing
          enrichment / truncation pipeline so the picker rows still
          carry price + market cap.
        * **Stage 2** — concurrent fan-out across
          :data:`_CONTRACT_LOOKUP_PLATFORMS` via ``asyncio.gather``.
          Used only when stage 1 returns no candidates. Each platform
          hits ``GET /coins/{platform}/contract/{address}``; 404
          (address not on that chain) is the expected miss shape and
          is swallowed silently. Any other per-chain error is logged
          and that chain drops out — other chains' hits still surface.
          The endpoint returns full coin metadata (price + market cap
          included), so candidates from this path skip the follow-up
          enrichment call.

        Multi-chain semantics: a deterministic-deploy token like USDC
        comes back from several platforms with the same
        ``coingecko_id``; dedup-by-id collapses it to a single picker
        row. The rare case where distinct coins share an address on
        different chains surfaces them as separate rows so the user
        disambiguates via price + market cap.
        """
        fuzzy = await self._search_fuzzy(address)
        if fuzzy:
            top = fuzzy[:_MAX_SEARCH_RESULTS]
            return await self._enrich_with_market_data(top)

        lookups = await asyncio.gather(
            *(self._lookup_contract(platform, address) for platform in _CONTRACT_LOOKUP_PLATFORMS),
            return_exceptions=True,
        )
        candidates: list[AssetCandidate] = []
        seen_ids: set[str] = set()
        for result in lookups:
            if isinstance(result, BaseException):
                _log.warning(
                    "CoinGecko per-platform contract lookup raised unexpectedly: %s",
                    result,
                )
                continue
            if result is None:
                continue
            if result.coingecko_id in seen_ids:
                continue
            seen_ids.add(result.coingecko_id)
            candidates.append(result)
        return candidates[:_MAX_SEARCH_RESULTS]

    async def _lookup_contract(
        self,
        platform: str,
        address: str,
    ) -> AssetCandidate | None:
        """``GET /coins/{platform}/contract/{address}`` → one candidate or ``None``.

        Used by :meth:`_search_by_contract` for the per-platform fan-out
        fallback. 404 (address not deployed on that chain) is the
        expected miss shape and resolves to ``None`` so ``gather``
        callers can simply skip it. Other failure modes (transport,
        5xx, redirect, non-JSON, malformed payload) are logged and
        also resolved to ``None`` — one flaky chain must not knock
        out the whole fan-out.

        The contract endpoint returns the same nested-shape payload as
        ``/coins/{id}`` (``market_data.current_price.usd``,
        ``market_data.market_cap.usd``); we opt out of localization,
        tickers, community, developer, and sparkline blocks to keep
        the response small.
        """
        endpoint = f"/coins/{platform}/contract"
        try:
            response = await self._client.get(
                f"{self._base_url}/coins/{platform}/contract/{address}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            _log.warning(
                "CoinGecko %s/%s transport error: %s",
                endpoint,
                address,
                e,
            )
            return None
        if response.status_code == 404:
            return None
        try:
            self._check_status(response, endpoint)
            payload = self._parse_json(response, endpoint)
        except MarketDataError as e:
            _log.warning(
                "CoinGecko %s/%s lookup failed: %s",
                endpoint,
                address,
                e,
            )
            return None
        if not isinstance(payload, dict):
            return None
        cg_id = payload.get("id")
        symbol = payload.get("symbol")
        name = payload.get("name")
        if not isinstance(cg_id, str) or not isinstance(symbol, str) or not isinstance(name, str):
            return None
        market_data = payload.get("market_data") or {}
        if not isinstance(market_data, dict):
            market_data = {}
        price_block = market_data.get("current_price") or {}
        cap_block = market_data.get("market_cap") or {}
        if not isinstance(price_block, dict):
            price_block = {}
        if not isinstance(cap_block, dict):
            cap_block = {}
        return AssetCandidate(
            coingecko_id=cg_id,
            symbol=symbol.upper(),
            name=name,
            market_cap_rank=payload.get("market_cap_rank"),
            current_price_usd=_safe_float(price_block.get("usd")),
            market_cap_usd=_safe_float(cap_block.get("usd")),
        )

    async def _search_fuzzy(self, query: str) -> list[AssetCandidate]:
        """``GET /search`` — fuzzy substring matching, ranked by market cap.

        Extracted so :meth:`search_assets` can run it concurrently
        with the exact-symbol probe via ``asyncio.gather``. Returns
        ALL coins from the response (caller decides on truncation
        after merging).
        """
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
            for c in coins
        ]

    async def _search_exact_symbol(self, query: str) -> list[AssetCandidate]:
        """``GET /coins/markets?symbols=<q>`` — exact ticker match, market-cap ordered.

        Returns at most ``_MAX_SEARCH_RESULTS`` listed coins whose
        symbol equals ``query`` (case-insensitive). CoinGecko handles
        the canonical-coin selection for popular tickers (e.g.
        ``symbols=btc`` returns only Bitcoin, not the dozens of
        unlisted scam BTC forks), so the merge logic in
        :meth:`search_assets` can prepend these directly without
        de-spam filtering.

        Defensive: a 2xx response with a non-list body shape is
        treated as "no exact matches" rather than crashing — keeps
        the picker working through any unannounced CoinGecko schema
        drift on this endpoint.
        """
        try:
            response = await self._client.get(
                f"{self._base_url}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "symbols": query.strip().lower(),
                    "order": "market_cap_desc",
                    "per_page": _MAX_SEARCH_RESULTS,
                    "page": 1,
                },
                headers=self._headers(),
            )
        except httpx.HTTPError as e:
            raise MarketDataError(f"CoinGecko transport error: {e}") from e
        self._check_status(response, "/coins/markets")
        payload = self._parse_json(response, "/coins/markets")
        if not isinstance(payload, list):
            return []
        return [
            AssetCandidate(
                coingecko_id=str(c["id"]),
                symbol=str(c["symbol"]).upper(),
                name=str(c["name"]),
                market_cap_rank=c.get("market_cap_rank"),
                current_price_usd=_safe_float(c.get("current_price")),
                market_cap_usd=_safe_float(c.get("market_cap")),
            )
            for c in payload
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
        """``GET /coins/{id}/market_chart/range`` → TWAP across a ±5min window.

        Returns the **time-weighted average** of all bucket prices in
        the window around ``when``, not a single nearest-by-timestamp
        pick. Rationale (the trader-UX argument):

        * **Bucket-alignment lottery is removed.** Under nearest-pick a
          user's outcome could flip between flat / won / lost purely on
          which 5-min bucket ``t_resolve`` happened to land in — a coin
          flip the user has zero control over. TWAP smooths that noise:
          a real +X% directional move stays ~+X%, a ±ε wobble averages
          to ~0%.
        * **TWAP is the institutional execution benchmark.** Disputing a
          single tick is easy; disputing "the time-weighted average over
          the ±5min window around your resolve time" is hard and
          replicable from any public chart in 10 seconds.
        * **Direction-symmetric.** No optimistic / pessimistic /
          long-vs-short branching: same formula for every claim.

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
          asset mutation. The TWAP is then computed over whichever stage
          produced data.

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

        Returned ``PricePoint``: ``requested_at = when``; ``returned_at``
        is the midpoint of the bucket span used to compute the TWAP
        (the effective "as-of" timestamp of the reported price).
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

        twap_price, midpoint_ms = self._twap(prices)
        returned_at = datetime.fromtimestamp(midpoint_ms / 1000, tz=UTC)
        return PricePoint(
            coingecko_id=coingecko_id,
            requested_at=when,
            returned_at=returned_at,
            price_usd=twap_price,
        )

    @staticmethod
    def _twap(prices: list[list[float]]) -> tuple[float, int]:
        """Trapezoidal time-weighted average over ``[ts_ms, price]`` buckets.

        Returns ``(twap_price_usd, midpoint_ms)``. The midpoint is the
        average of the first and last bucket timestamps — i.e. the
        effective "as-of" time of the integral. Callers expose it on
        ``PricePoint.returned_at`` so the outcomes table's audit
        references (``price_t0_ref`` / ``price_resolve_ref``) still
        anchor each TWAP to a concrete instant for replay.

        Math: standard trapezoidal-rule integral of a piecewise-linear
        price curve divided by its time span. For uniformly-spaced
        buckets this reduces to the arithmetic mean with halved
        endpoints — i.e. close to ``mean(prices)`` — which is what
        you'd get from CoinGecko's evenly-stepped grid in 99% of
        calls. The trapezoidal formula stays correct when CoinGecko
        returns a non-uniform grid (e.g. a transient data gap leaves
        an isolated bucket on one side of the window) — each bucket
        is weighted by the time interval it actually represents, not
        by its position in the list.

        Edge cases:

        * **Single bucket**: TWAP is that price; midpoint is that
          bucket's timestamp. Degenerate but honest — with one data
          point there's no integral to compute.
        * **All same-timestamp buckets** (defensive; CoinGecko doesn't
          emit this shape but a future data source might): fall back
          to the arithmetic mean of the prices and use the shared
          timestamp as the midpoint. Prevents a divide-by-zero on the
          time span.
        """
        sorted_prices = sorted(prices, key=lambda p: p[0])
        first_ts_ms = int(sorted_prices[0][0])
        last_ts_ms = int(sorted_prices[-1][0])
        if len(sorted_prices) == 1:
            return float(sorted_prices[0][1]), first_ts_ms
        span_ms = last_ts_ms - first_ts_ms
        midpoint_ms = (first_ts_ms + last_ts_ms) // 2
        if span_ms == 0:
            mean_price = sum(float(p[1]) for p in sorted_prices) / len(sorted_prices)
            return mean_price, first_ts_ms
        total_area = 0.0
        for (ts_a, p_a), (ts_b, p_b) in pairwise(sorted_prices):
            dt = float(ts_b - ts_a)
            total_area += 0.5 * (float(p_a) + float(p_b)) * dt
        twap = total_area / float(span_ms)
        return twap, midpoint_ms

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
