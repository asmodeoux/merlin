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


def _stub_markets(*coins: dict[str, object]) -> respx.Route:
    """Mock the ``/coins/markets`` endpoint (the exact-symbol probe).

    ``search_assets`` fans out to BOTH ``/search`` and
    ``/coins/markets?symbols=<q>`` in parallel; ``respx.mock`` asserts
    on unmocked routes, so every search test that exercises the
    ticker-shaped happy path must stub this endpoint too. Pass an
    empty arg list for "no exact-symbol matches" (the common case in
    failure-mode tests where the test only cares about ``/search``
    blowing up).
    """
    return respx.get(f"{_BASE_URL}/coins/markets").mock(
        return_value=httpx.Response(200, json=list(coins))
    )


@respx.mock
async def test_search_assets_success_returns_top_candidates() -> None:
    """Happy path locks the parse logic + market-cap ordering.

    Also pins the dedup contract: a popular ticker like ``BTC``
    surfaces in both ``/search`` (as the top fuzzy hit) and
    ``/coins/markets?symbols=btc`` (as the only exact-symbol hit).
    The merge prepends the exact match and skips the duplicate in
    the fuzzy results — Bitcoin must appear exactly once.
    """
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
    _stub_markets(
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
    )
    out = await _adapter().search_assets("btc")
    assert [c.coingecko_id for c in out] == ["bitcoin", "ethereum"]
    assert [c.symbol for c in out] == ["BTC", "ETH"]


@respx.mock
async def test_search_assets_surfaces_exact_match_only_in_markets_endpoint() -> None:
    """The live "H → Humanity" regression: ``/search`` doesn't include it.

    Reproducing the actual CoinGecko behaviour observed in production:
    ``/search?query=H`` returns 25 coins whose names / symbols merely
    *contain* the letter "h" (ETH, USDT, FIGR_HELOC, HYPE, ZEC, ...).
    The legitimate ``symbol=H`` coin (Humanity Protocol, rank ~161)
    never appears in that response at all because single-letter
    queries fan out against the entire 17k+ coin universe and
    CoinGecko caps the result list.

    The exact-symbol endpoint (``/coins/markets?symbols=h``) returns
    Humanity directly. ``search_assets`` must call both endpoints,
    prepend the exact match, and serve a top-5 with Humanity at
    index 0 — the user's intent (typing the exact ticker) is
    respected even though it'd be invisible to ``/search`` alone.

    This is the test that would have caught the bug in the first
    place — the previous fix only re-ordered ``/search`` results and
    couldn't help when ``/search`` didn't surface Humanity at all.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                    {"id": "tether", "symbol": "usdt", "name": "Tether", "market_cap_rank": 3},
                    {
                        "id": "figure-heloc",
                        "symbol": "figr_heloc",
                        "name": "Figure Heloc",
                        "market_cap_rank": 9,
                    },
                    {
                        "id": "hyperliquid",
                        "symbol": "hype",
                        "name": "Hyperliquid",
                        "market_cap_rank": 10,
                    },
                    {"id": "zcash", "symbol": "zec", "name": "Zcash", "market_cap_rank": 15},
                ]
            },
        )
    )
    _stub_markets(
        {"id": "humanity", "symbol": "h", "name": "Humanity", "market_cap_rank": 161},
    )
    out = await _adapter().search_assets("H")
    assert out[0].coingecko_id == "humanity"
    assert out[0].symbol == "H"
    assert [c.coingecko_id for c in out[1:]] == [
        "ethereum",
        "tether",
        "figure-heloc",
        "hyperliquid",
    ]


@respx.mock
async def test_search_assets_exact_match_lowercase_query_still_surfaced() -> None:
    """Case-insensitive on both endpoints — users type ``h`` and ``H`` interchangeably.

    The adapter lowercases the query when sending ``?symbols=`` and
    uppercases all returned symbols at parse time, so the merge / dedup
    works whichever casing the user typed.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                ]
            },
        )
    )
    _stub_markets(
        {"id": "humanity", "symbol": "h", "name": "Humanity", "market_cap_rank": 161},
    )
    out = await _adapter().search_assets("h")
    assert out[0].coingecko_id == "humanity"


@respx.mock
async def test_search_assets_dedupes_exact_match_already_in_fuzzy() -> None:
    """When the exact match also appears in ``/search`` results, no duplicate.

    Popular tickers like ``BTC`` surface in both endpoints — ``/search``
    returns Bitcoin as its top fuzzy hit, and
    ``/coins/markets?symbols=btc`` returns Bitcoin as its only exact
    hit. The merge dedupes by ``coingecko_id``: Bitcoin appears
    exactly once and the remaining 4 slots fill from the fuzzy
    results.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                    {
                        "id": "bitcoin-cash",
                        "symbol": "bch",
                        "name": "Bitcoin Cash",
                        "market_cap_rank": 26,
                    },
                ]
            },
        )
    )
    _stub_markets(
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
    )
    out = await _adapter().search_assets("BTC")
    coin_ids = [c.coingecko_id for c in out]
    assert coin_ids.count("bitcoin") == 1
    assert coin_ids == ["bitcoin", "ethereum", "bitcoin-cash"]


@respx.mock
async def test_search_assets_no_exact_match_preserves_fuzzy_order() -> None:
    """No exact-symbol hits → result is the fuzzy list in CoinGecko's native order.

    A name-shaped query like ``"bitcoin"`` triggers both endpoints
    (no whitespace so :func:`_looks_like_ticker` is True), but
    ``/coins/markets?symbols=bitcoin`` returns ``[]`` because no coin
    has the symbol "bitcoin". The merge falls through to fuzzy
    results unchanged.
    """
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
    _stub_markets()
    out = await _adapter().search_assets("bitcoin")
    assert [c.coingecko_id for c in out] == ["bitcoin", "ethereum"]


@respx.mock
async def test_search_assets_skips_markets_probe_when_query_has_whitespace() -> None:
    """Multi-word queries skip the exact-symbol probe entirely.

    No coin has a multi-word ticker, so calling
    ``/coins/markets?symbols=bitcoin%20cash`` is guaranteed-empty and
    a pure waste of the Demo-tier CoinGecko quota. The heuristic in
    :func:`_looks_like_ticker` short-circuits to ``/search``-only.

    Pinned by asserting the markets route is never called with
    ``symbols=`` (the exact-symbol probe shape). The follow-up
    enrichment pass keyed by ``ids=`` is a different code path and
    legitimately fires for any candidate the fuzzy ``/search`` hop
    surfaces without price / market cap — that's covered separately
    by :func:`test_search_assets_enriches_fuzzy_path_with_market_data`.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "bitcoin-cash",
                        "symbol": "bch",
                        "name": "Bitcoin Cash",
                        "market_cap_rank": 26,
                    },
                ]
            },
        )
    )
    markets_route = _stub_markets()
    out = await _adapter().search_assets("bitcoin cash")
    assert [c.coingecko_id for c in out] == ["bitcoin-cash"]
    for call in markets_route.calls:
        params = call.request.url.params
        assert "symbols" not in params, (
            "the exact-symbol probe must not fire for multi-word queries — "
            f"got params={dict(params)}"
        )


@respx.mock
async def test_search_assets_markets_endpoint_failure_falls_back_to_fuzzy() -> None:
    """A ``/coins/markets`` outage must not knock out the picker.

    The exact-symbol probe is a quality-of-life addition; ``/search``
    remains the primary endpoint. If ``/coins/markets`` returns 503
    (or any other ``MarketDataError``), the adapter logs and returns
    fuzzy-only results — the user still gets a usable picker even
    if low-cap exact-symbol hits are temporarily invisible.

    The defensive :func:`_prioritise_exact_ticker` pass also kicks
    in here: if the fuzzy results happen to contain an exact symbol
    match for the query, it's still floated to the top even though
    the markets endpoint failed.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                ]
            },
        )
    )
    respx.get(f"{_BASE_URL}/coins/markets").mock(
        return_value=httpx.Response(503, text="markets endpoint down")
    )
    out = await _adapter().search_assets("ETH")
    assert [c.coingecko_id for c in out] == ["ethereum"]


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
    _stub_markets()
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
    _stub_markets()
    with pytest.raises(MarketDataError) as exc:
        await _adapter().search_assets("btc")
    msg = str(exc.value)
    assert "non-JSON" in msg
    assert "200" in msg
    assert "<html>" in msg


@respx.mock
async def test_search_assets_5xx_raises_market_data_error() -> None:
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(503, text="upstream down"))
    _stub_markets()
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
    """Empty ±5min + populated ±30min → return the TWAP across the wide window.

    Pins the SOL-bet recovery scenario: CoinGecko's narrow lookup
    returns no buckets (sparsity around the exact minute) but a wider
    lookup catches some. The adapter returns the time-weighted
    average across the available buckets rather than rejecting the
    bet.
    """
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    narrow_from = int((when - timedelta(minutes=5)).timestamp())
    wide_from = int((when - timedelta(minutes=30)).timestamp())
    # The narrow window returns empty; the wide window catches two
    # buckets within ±30min around ``when`` (outside ±5min).
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
    # Trapezoidal TWAP across the two buckets in the wide-window slice:
    # 0.5 * (140.0 + 142.5) = 141.25.
    assert pp.price_usd == pytest.approx(141.25)
    assert pp.coingecko_id == "solana"


@respx.mock
async def test_get_price_at_returns_twap_over_buckets() -> None:
    """Three buckets at non-uniform spacing → time-weighted average, not nearest.

    Replaces the previously-pinned nearest-bucket contract. Under
    nearest-pick, ``when`` was ~30s after the middle bucket so the
    return would have been 101.5 — but two seconds of clock skew
    could push it onto either the -4min or +4.5min bucket and flip
    the outcome on threshold-near calls. The TWAP collapses that
    bucket-alignment lottery into a single deterministic answer.

    Math (trapezoidal rule over the bucket span):
      - Sorted ts offsets: -240s, -30s, +270s — span 510s.
      - Trapezoid 1: 0.5 · (100.0 + 101.5) · 210s = 21157.5
      - Trapezoid 2: 0.5 · (101.5 + 102.0) · 300s = 30525.0
      - TWAP = 51682.5 / 510 ≈ 101.34.
    """
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
    assert pp.price_usd == pytest.approx(101.3382, rel=1e-4)
    # ``returned_at`` is the midpoint of the bucket span (the as-of
    # timestamp the TWAP corresponds to). For the offsets above the
    # midpoint is at +15s relative to ``when``.
    assert pp.returned_at == datetime.fromtimestamp((target_ms + 15_000) / 1000, tz=UTC)


@respx.mock
async def test_get_price_at_twap_smooths_noise_below_threshold() -> None:
    """The [99.8, 100.1, 100.6] scenario evenly straddling t_resolve.

    Encodes the user's exact question: a long call entered at $100,
    resolution returns these three buckets centred on ``t_resolve``.
    Under nearest-pick the outcome would have been a coin flip
    between flat (closest = 99.8 or 100.1) and won (closest = 100.6)
    depending purely on bucket alignment. Under TWAP the answer is
    deterministic: 0.5·(99.8+100.1)·dt + 0.5·(100.1+100.6)·dt over
    2·dt = (99.95 + 100.35)/2 = 100.15 → +0.15% return → flat. The
    trader's directional read was correct, but the actual move stayed
    inside noise — and the resolver no longer rewards / punishes a
    threshold-crossing bucket that the user couldn't see.
    """
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            json={
                "prices": [
                    [target_ms - 300_000, 99.8],
                    [target_ms, 100.1],
                    [target_ms + 300_000, 100.6],
                ]
            },
        )
    )
    pp = await _adapter().get_price_at("bitcoin", when)
    assert pp.price_usd == pytest.approx(100.15)


@respx.mock
async def test_get_price_at_twap_single_bucket_returns_that_price() -> None:
    """Degenerate n=1 case: TWAP collapses to the only bucket.

    With one data point there's no integral to compute. The adapter
    must NOT raise; it returns the single price honestly and
    surfaces that bucket's timestamp as the ``returned_at`` so
    audits show exactly which point was used.
    """
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            json={"prices": [[target_ms - 60_000, 101.7]]},
        )
    )
    pp = await _adapter().get_price_at("bitcoin", when)
    assert pp.price_usd == pytest.approx(101.7)
    assert pp.returned_at == datetime.fromtimestamp((target_ms - 60_000) / 1000, tz=UTC)


@respx.mock
async def test_get_price_at_twap_handles_non_uniform_spacing() -> None:
    """Non-uniform bucket spacing must weight by interval, not by count.

    Three buckets: 100.0 at -600s, 100.0 at -30s, 110.0 at 0s.
    Naive arithmetic mean would give (100+100+110)/3 ≈ 103.33,
    over-weighting the brief 10% spike at the end. Trapezoidal TWAP:

      - Trap 1: 0.5·(100.0 + 100.0)·570s = 57000.0
      - Trap 2: 0.5·(100.0 + 110.0)·30s = 3150.0
      - TWAP = 60150.0 / 600s = 100.25

    Pins the property that a transient spike on the boundary of the
    window CAN'T move the TWAP by more than the spike's time-share
    of the window — exactly the protection trader UX wants against
    end-of-window wicks.
    """
    when = datetime(2026, 6, 2, 19, 41, 18, tzinfo=UTC)
    target_ms = int(when.timestamp() * 1000)
    respx.get(f"{_BASE_URL}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            json={
                "prices": [
                    [target_ms - 600_000, 100.0],
                    [target_ms - 30_000, 100.0],
                    [target_ms, 110.0],
                ]
            },
        )
    )
    pp = await _adapter().get_price_at("bitcoin", when)
    assert pp.price_usd == pytest.approx(100.25)


# ---------------------------------------------------------------------------
# Picker enrichment — price + market cap propagated to AssetCandidate
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_assets_extracts_price_and_cap_from_exact_probe() -> None:
    """``/coins/markets?symbols=`` already returns price + market cap.

    The exact-symbol path was a single endpoint already; the picker
    just needs us to pull two more fields out of the payload. Zero
    additional CoinGecko calls — this is the cheap branch.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    _stub_markets(
        {
            "id": "bitcoin",
            "symbol": "btc",
            "name": "Bitcoin",
            "market_cap_rank": 1,
            "current_price": 64500.12,
            "market_cap": 1_270_000_000_000,
        },
    )
    out = await _adapter().search_assets("btc")
    assert len(out) == 1
    assert out[0].current_price_usd == pytest.approx(64500.12)
    assert out[0].market_cap_usd == pytest.approx(1_270_000_000_000)


@respx.mock
async def test_search_assets_enriches_fuzzy_path_with_market_data() -> None:
    """Fuzzy ``/search`` lacks price/cap — enrichment fills them in.

    Live UX bug: three distinct ``coingecko_id``s for "STABLE — Stable"
    that look identical in the picker. The dedup-by-coingecko_id
    correctly keeps all three (they ARE different coins). The fix
    differentiates them by surfacing ``current_price_usd`` +
    ``market_cap_usd``, which the fuzzy endpoint doesn't include.

    The adapter follows up with a single
    ``/coins/markets?ids=<id1,id2,...>`` enrichment call to fill the
    gap. Pinned here: the call happens with the expected ids and the
    returned market data lands on the matching candidates.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "stable", "symbol": "stable", "name": "Stable", "market_cap_rank": 800},
                    {
                        "id": "stable-2",
                        "symbol": "stable",
                        "name": "Stable",
                        "market_cap_rank": 1500,
                    },
                    {
                        "id": "stable-3",
                        "symbol": "stable",
                        "name": "STABLE",
                        "market_cap_rank": None,
                    },
                ]
            },
        )
    )
    markets_route = respx.get(f"{_BASE_URL}/coins/markets").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=(
                # The exact-symbol probe (symbols=stable) returns
                # nothing — the picker is entirely fed by the fuzzy
                # path here, which is the scenario that needs
                # enrichment.
                []
                if "symbols" in request.url.params
                else [
                    {
                        "id": "stable",
                        "symbol": "stable",
                        "name": "Stable",
                        "current_price": 0.87,
                        "market_cap": 5_200_000,
                    },
                    {
                        "id": "stable-2",
                        "symbol": "stable",
                        "name": "Stable",
                        "current_price": 0.21,
                        "market_cap": 200_000,
                    },
                    {
                        "id": "stable-3",
                        "symbol": "stable",
                        "name": "STABLE",
                        "current_price": None,
                        "market_cap": None,
                    },
                ]
            ),
        )
    )
    out = await _adapter().search_assets("stable")
    coingecko_ids = [c.coingecko_id for c in out]
    assert coingecko_ids == ["stable", "stable-2", "stable-3"], (
        "distinct coingecko_ids that share (symbol, name) must all "
        "survive the merge — dedup is by coingecko_id, not by display string"
    )
    by_id = {c.coingecko_id: c for c in out}
    assert by_id["stable"].current_price_usd == pytest.approx(0.87)
    assert by_id["stable"].market_cap_usd == pytest.approx(5_200_000)
    assert by_id["stable-2"].current_price_usd == pytest.approx(0.21)
    assert by_id["stable-2"].market_cap_usd == pytest.approx(200_000)
    # Long-tail coin with null market data — surfaces as ``None`` on the
    # candidate so the bot renders the row without crashing.
    assert by_id["stable-3"].current_price_usd is None
    assert by_id["stable-3"].market_cap_usd is None
    # One probe call (symbols=stable) + one enrichment call (ids=...).
    assert markets_route.call_count == 2
    enrichment_calls = [call for call in markets_route.calls if "ids" in call.request.url.params]
    assert len(enrichment_calls) == 1
    ids_param = enrichment_calls[0].request.url.params["ids"]
    assert set(ids_param.split(",")) == {"stable", "stable-2", "stable-3"}


@respx.mock
async def test_search_assets_enrichment_failure_degrades_gracefully() -> None:
    """A 503 on the enrichment call must NOT take out the picker.

    The picker still renders without price/cap (the bot shows ``—``
    in those columns). Knocking the whole search out because the
    secondary endpoint is down would be a regression on the same
    "graceful degradation" principle that motivated the exact-probe
    failure handling.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2},
                ]
            },
        )
    )

    def _markets_handler(request: httpx.Request) -> httpx.Response:
        if "symbols" in request.url.params:
            # Probe returns empty so the fuzzy path is the only source.
            return httpx.Response(200, json=[])
        # Enrichment call (ids=...) fails — must not propagate.
        return httpx.Response(503, text="enrichment down")

    respx.get(f"{_BASE_URL}/coins/markets").mock(side_effect=_markets_handler)
    out = await _adapter().search_assets("ethereum")
    assert [c.coingecko_id for c in out] == ["ethereum"]
    assert out[0].current_price_usd is None
    assert out[0].market_cap_usd is None


@respx.mock
async def test_search_assets_keeps_distinct_lookalike_coins() -> None:
    """Live "STABLE x3" regression: dedup is by coingecko_id, not display.

    Three rows with the same ``(symbol, name)`` but distinct
    ``coingecko_id`` are three separate coins on CoinGecko. The
    picker must surface all three so the user can disambiguate (via
    the price + market cap the enrichment pass populates).
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "stable", "symbol": "stable", "name": "Stable"},
                    {"id": "stable-2", "symbol": "stable", "name": "Stable"},
                    {"id": "stable-3", "symbol": "stable", "name": "Stable"},
                ]
            },
        )
    )
    respx.get(f"{_BASE_URL}/coins/markets").mock(return_value=httpx.Response(200, json=[]))
    out = await _adapter().search_assets("stable")
    assert [c.coingecko_id for c in out] == ["stable", "stable-2", "stable-3"]


@respx.mock
async def test_search_assets_handles_null_price_in_markets_probe() -> None:
    """A null ``current_price`` / ``market_cap`` must coerce to ``None``.

    CoinGecko legitimately emits ``null`` for these on delisted /
    halted coins. The parser must not crash; the bot renders ``—``
    for those cells.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    _stub_markets(
        {
            "id": "halted-coin",
            "symbol": "hlt",
            "name": "Halted",
            "market_cap_rank": None,
            "current_price": None,
            "market_cap": None,
        },
    )
    out = await _adapter().search_assets("hlt")
    assert len(out) == 1
    assert out[0].current_price_usd is None
    assert out[0].market_cap_usd is None


# ---------------------------------------------------------------------------
# EVM contract-address dispatch — search_assets now accepts ``0x…`` pastes
# ---------------------------------------------------------------------------


# A canonical EVM address shape (40 lowercase hex after ``0x``). Used by
# the contract-address test cases below. NOT the real USDC address —
# tests must not encode a live-token contract because that creates a
# false "is this address still listed?" coupling at test time.
_FAKE_ADDR = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

# EVM fan-out shape, used to build per-platform respx routes in the
# fallback tests. Mirrors ``_EVM_LOOKUP_PLATFORMS`` in the adapter;
# if the adapter list changes, this list changes in lockstep (the
# tests would otherwise leave unmocked routes that respx flags).
_FAN_OUT_PLATFORMS = (
    "ethereum",
    "binance-smart-chain",
    "base",
    "arbitrum-one",
)


def _contract_payload(
    *,
    coingecko_id: str,
    symbol: str,
    name: str,
    price_usd: float | None = None,
    market_cap_usd: float | None = None,
    market_cap_rank: int | None = None,
) -> dict[str, object]:
    """Shape a ``/coins/{platform}/contract/{address}`` 200 payload.

    The contract endpoint returns the same nested ``market_data``
    shape as ``/coins/{id}``: price + market cap live under
    ``market_data.current_price.usd`` and
    ``market_data.market_cap.usd``. The adapter's ``_lookup_contract``
    coerces these via ``_safe_float`` and tolerates ``null`` blocks,
    so the helper mirrors that shape exactly.
    """
    return {
        "id": coingecko_id,
        "symbol": symbol,
        "name": name,
        "market_cap_rank": market_cap_rank,
        "market_data": {
            "current_price": {"usd": price_usd} if price_usd is not None else {},
            "market_cap": {"usd": market_cap_usd} if market_cap_usd is not None else {},
        },
    }


def _mock_all_platforms_404(address: str) -> dict[str, respx.Route]:
    """Stub every fan-out platform to 404 by default.

    Tests that want a single chain to win override its route after
    calling this helper. Returns the per-platform route map so
    callers can inspect ``call_count`` / ``calls`` on the
    not-overridden routes.
    """
    return {
        platform: respx.get(
            f"{_BASE_URL}/coins/{platform}/contract/{address}",
        ).mock(return_value=httpx.Response(404, json={"error": "coin not found"}))
        for platform in _FAN_OUT_PLATFORMS
    }


@pytest.mark.parametrize(
    "query, expected",
    [
        # Bare address — the primary user input shape.
        (_FAKE_ADDR, _FAKE_ADDR),
        # EIP-55 mixed-case is the canonical wallet display format;
        # the adapter normalises to lowercase so the downstream
        # CoinGecko call hits the canonical key.
        (
            "0xA0b86991c6218B36c1D19D4a2e9Eb0cE3606eB48",
            _FAKE_ADDR,
        ),
        # Embedded in an explorer URL paste.
        (
            f"https://etherscan.io/token/{_FAKE_ADDR}",
            _FAKE_ADDR,
        ),
        # Embedded in prose ("check this token: 0x…").
        (f"check this token: {_FAKE_ADDR}", _FAKE_ADDR),
    ],
)
def test_extract_evm_address_happy_paths(query: str, expected: str) -> None:
    """The pure helper accepts every shape the adapter dispatcher relies on."""
    from merlin_infrastructure.market_data.coingecko import _extract_evm_address

    assert _extract_evm_address(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "BTC",
        "bitcoin",
        "ETH long 2 weeks",
        # Too short (39 hex digits — not a valid EVM address shape).
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb4",
        # Wrong prefix — Solana-style base58 doesn't match.
        "FoNGoB2WMyEZeFwUmpQjzmJBp5RxXKqL3hPpkB78mURn",
        # ``0x``-prefixed but with non-hex chars in the body.
        "0xGGGG6991c6218b36c1d19d4a2e9eb0ce3606eb48",
    ],
)
def test_extract_evm_address_rejects(query: str) -> None:
    """Plain tickers / coin names / wrong-chain addresses fall through."""
    from merlin_infrastructure.market_data.coingecko import _extract_evm_address

    assert _extract_evm_address(query) is None


@respx.mock
async def test_search_assets_routes_contract_address_to_search_first() -> None:
    """A 0x-address pastes goes through ``/search`` and the per-platform
    fan-out NEVER fires when ``/search`` returns hits.

    The contract-aware path piggy-backs on CoinGecko's general
    search index (which covers "name, symbol or contract address"),
    so popular tokens like USDC surface in a single hop. Pinning
    this is important — the fan-out costs 5 CoinGecko calls and we
    must not burn that quota when the cheap path works.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {
                        "id": "usd-coin",
                        "symbol": "usdc",
                        "name": "USD Coin",
                        "market_cap_rank": 7,
                    },
                ]
            },
        )
    )
    # Enrichment call covers the ``ids=`` shape (fuzzy results lack
    # price/cap). Return a payload that lets us pin enrichment ran.
    respx.get(f"{_BASE_URL}/coins/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "usd-coin",
                    "symbol": "usdc",
                    "name": "USD Coin",
                    "current_price": 1.0,
                    "market_cap": 60_000_000_000,
                },
            ],
        )
    )
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)

    out = await _adapter().search_assets(_FAKE_ADDR)

    assert [c.coingecko_id for c in out] == ["usd-coin"]
    assert out[0].current_price_usd == pytest.approx(1.0)
    assert out[0].market_cap_usd == pytest.approx(60_000_000_000)
    # No per-platform contract call fired — ``/search`` covered it.
    for platform, route in platform_routes.items():
        assert route.call_count == 0, (
            f"per-platform fallback must not fire when /search has hits — "
            f"{platform} was called {route.call_count}x"
        )


@respx.mock
async def test_search_assets_falls_back_to_contract_endpoint_when_search_misses() -> None:
    """When ``/search`` returns no coins, the adapter fans out across
    the four popular EVM chains and surfaces whichever one wins.

    The contract endpoint returns full market data (price + market
    cap) so the resulting candidate is already enriched and the
    adapter skips the follow-up ``ids=`` enrichment call.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)
    # One chain wins — pretend the address is a Base-only token.
    platform_routes["base"].mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="long-tail-base-token",
                symbol="ltb",
                name="Long Tail",
                price_usd=0.0035,
                market_cap_usd=125_000,
                market_cap_rank=4123,
            ),
        )
    )

    out = await _adapter().search_assets(_FAKE_ADDR)

    assert [c.coingecko_id for c in out] == ["long-tail-base-token"]
    assert out[0].symbol == "LTB"
    assert out[0].current_price_usd == pytest.approx(0.0035)
    assert out[0].market_cap_usd == pytest.approx(125_000)
    assert out[0].market_cap_rank == 4123
    # All four EVM chains were probed concurrently — pin the fan-out
    # shape so a future refactor can't silently regress to a serial
    # loop.
    for platform, route in platform_routes.items():
        assert route.call_count == 1, (
            f"every chain must be probed exactly once in the fan-out; "
            f"{platform} fired {route.call_count} times"
        )


@respx.mock
async def test_search_assets_contract_fallback_dedupes_multichain_tokens() -> None:
    """A deterministic-deploy token like USDC surfaces from multiple
    chains with the same ``coingecko_id``. Dedup collapses it to one row.

    This is the "USDC on ETH/Polygon/Base/Arbitrum all return
    ``id=usd-coin``" scenario. Without dedup the picker would show
    USDC up to 5 times — confusing and useless. With dedup it shows
    once with the best market data the chain payloads carry.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)
    multichain_payload = _contract_payload(
        coingecko_id="usd-coin",
        symbol="usdc",
        name="USD Coin",
        price_usd=1.0,
        market_cap_usd=60_000_000_000,
        market_cap_rank=7,
    )
    # Three of the four EVM chains return the same coingecko_id.
    platform_routes["ethereum"].mock(return_value=httpx.Response(200, json=multichain_payload))
    platform_routes["base"].mock(return_value=httpx.Response(200, json=multichain_payload))
    platform_routes["arbitrum-one"].mock(return_value=httpx.Response(200, json=multichain_payload))

    out = await _adapter().search_assets(_FAKE_ADDR)

    assert [c.coingecko_id for c in out] == ["usd-coin"], (
        "multi-chain hits with the same coingecko_id must collapse to one row"
    )
    assert out[0].current_price_usd == pytest.approx(1.0)


@respx.mock
async def test_search_assets_contract_fallback_surfaces_distinct_coins_sharing_address() -> None:
    """Different coingecko_ids on different chains → separate picker rows.

    The rare-collision case: two unrelated projects happen to deploy
    at the same EVM address on different chains (coincidence or
    deliberate squatting). The adapter must surface both so the user
    disambiguates via price + market cap; collapsing distinct coins
    would silently pick a winner the user didn't choose.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)
    platform_routes["ethereum"].mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="coin-on-eth",
                symbol="cone",
                name="Coin On ETH",
                price_usd=2.50,
                market_cap_usd=12_000_000,
            ),
        )
    )
    platform_routes["binance-smart-chain"].mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="coin-on-bsc",
                symbol="conb",
                name="Coin On BSC",
                price_usd=0.0001,
                market_cap_usd=3_000,
            ),
        )
    )

    out = await _adapter().search_assets(_FAKE_ADDR)
    ids = {c.coingecko_id for c in out}
    assert ids == {"coin-on-eth", "coin-on-bsc"}, (
        "distinct coingecko_ids sharing an address across chains must "
        "both surface; the user disambiguates via price + market cap"
    )


@respx.mock
async def test_search_assets_contract_fallback_swallows_404_and_5xx() -> None:
    """One winning chain + three failing chains → only the winner is returned.

    404s are the expected "address not on this chain" miss shape;
    5xx / transport failures on other chains are logged but never
    take out the one chain that DID find the token. The user gets a
    working picker row instead of an "everything is broken" error.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)
    # Override two chains with non-404 failures — neither should crash
    # the gather; the surviving 404 + the one winner must compose
    # into a single-row result.
    platform_routes["base"].mock(return_value=httpx.Response(503, text="upstream down"))
    platform_routes["binance-smart-chain"].mock(
        return_value=httpx.Response(
            200, text="<html>WAF block</html>", headers={"content-type": "text/html"}
        )
    )
    platform_routes["arbitrum-one"].mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="winner",
                symbol="win",
                name="Winner",
                price_usd=42.0,
                market_cap_usd=4_200_000,
            ),
        )
    )

    out = await _adapter().search_assets(_FAKE_ADDR)
    assert [c.coingecko_id for c in out] == ["winner"]


@respx.mock
async def test_search_assets_extracts_address_embedded_in_explorer_url() -> None:
    """An Etherscan-style URL paste routes through the contract path.

    The regex matches the address anywhere in the input string, so a
    user pastes ``https://etherscan.io/token/0xabc…`` and the
    adapter peels out the address and dispatches the contract
    pipeline. Pinned by mocking the contract endpoints and confirming
    they fire — not the fuzzy / exact ticker probe.
    """
    explorer_paste = f"https://etherscan.io/token/{_FAKE_ADDR}"
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    platform_routes = _mock_all_platforms_404(_FAKE_ADDR)
    platform_routes["ethereum"].mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="explorer-token",
                symbol="exp",
                name="Explorer Token",
                price_usd=12.34,
                market_cap_usd=1_500_000,
            ),
        )
    )

    out = await _adapter().search_assets(explorer_paste)

    assert [c.coingecko_id for c in out] == ["explorer-token"]
    # The exact-symbol ticker probe (``symbols=`` on ``/coins/markets``)
    # must not fire — the dispatcher took the contract branch.
    # Nothing else mocked on ``/coins/markets`` means respx would
    # have errored on any such call, so the absence of an assertion
    # here IS the assertion. We just confirm at least one platform
    # was probed.
    assert platform_routes["ethereum"].call_count == 1


@respx.mock
async def test_search_assets_treats_plain_ticker_unchanged() -> None:
    """A plain ticker like ``BTC`` does NOT route through the contract path.

    Regression guard: any future tweak to the dispatcher that
    inadvertently routes non-addresses through ``_search_by_contract``
    would burn 4 CoinGecko calls per pick and break the existing
    ticker / fuzzy semantics.
    """
    respx.get(f"{_BASE_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
                ]
            },
        )
    )
    _stub_markets(
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1},
    )
    # No contract endpoints are mocked. respx would raise on an
    # unexpected call, so any future regression that routes "BTC"
    # through ``_search_by_contract`` fails this test loudly.

    out = await _adapter().search_assets("BTC")
    assert [c.coingecko_id for c in out] == ["bitcoin"]


# ---------------------------------------------------------------------------
# Solana base58 contract-address dispatch
# ---------------------------------------------------------------------------


# A canonical Solana address shape (43 base58 chars, no zero / O / I / l).
# Doesn't correspond to any real token — tests must not encode a live
# token's address.
_FAKE_SOLANA_ADDR = "DF12JV9hFJbMWFuG7ff1wqaydj6HSkvwyt6mJnNVuGiR"


@pytest.mark.parametrize(
    "query, expected",
    [
        # Bare base58 address.
        (_FAKE_SOLANA_ADDR, _FAKE_SOLANA_ADDR),
        # Embedded in a Solscan-style URL paste.
        (f"https://solscan.io/token/{_FAKE_SOLANA_ADDR}", _FAKE_SOLANA_ADDR),
        # 32-char program-derived addresses also count.
        ("11111111111111111111111111111112", "11111111111111111111111111111112"),
    ],
)
def test_extract_solana_address_happy_paths(query: str, expected: str) -> None:
    """The pure helper accepts every shape the adapter dispatcher relies on."""
    from merlin_infrastructure.market_data.coingecko import _extract_solana_address

    assert _extract_solana_address(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        # Plain tickers and coin names — far below the 32-char floor.
        "",
        "BTC",
        "ETH",
        "Bitcoin",
        "Solana",
        # CoinGecko URL — broken into sub-32 chunks by '/', '.', ':'
        "https://www.coingecko.com/en/coins/bitcoin",
    ],
)
def test_extract_solana_address_rejects_non_address_shapes(query: str) -> None:
    """Tickers, coin names, and CoinGecko URLs fall through.

    NB: this helper does NOT guarantee rejection of EVM addresses
    in isolation — some EVM addresses (e.g. PEPE,
    ``0x6982…1933``) carry a 32+ char zero-free hex window that
    satisfies the base58 alphabet, so the regex would happily
    match. EVM safety is enforced upstream by the dispatcher
    checking :func:`_extract_evm_address` *first*; see
    :func:`test_evm_address_with_solana_shaped_hex_body_still_routes_to_evm`
    which pins that ordering.
    """
    from merlin_infrastructure.market_data.coingecko import _extract_solana_address

    assert _extract_solana_address(query) is None


@respx.mock
@pytest.mark.parametrize(
    "evm_address",
    [
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "0xdac17f958d2ee523a2206206994597c13d831ec7",
        # PEPE — its hex body has a 34-char zero-free substring that
        # the base58 regex would happily match in isolation. The
        # EVM-first dispatch is what saves us; this test pins that
        # ordering rather than the regex's standalone behaviour.
        "0x6982508145454ce325ddbe47a25d4ec3d2311933",
    ],
)
async def test_evm_address_with_solana_shaped_hex_body_still_routes_to_evm(
    evm_address: str,
) -> None:
    """EVM dispatch must fire BEFORE Solana dispatch.

    Empirically, the PEPE contract has a 34-char zero-free substring
    in its hex body that matches the Solana base58 regex. Routing
    that to ``/coins/solana/contract/{partial-hex}`` would burn an
    API call and surface a misleading "no Solana token at this
    address" result. The dispatcher's EVM-first ordering means the
    Solana regex never sees the input — pinned here by mounting the
    EVM fan-out (which must fire) and watching for any Solana route
    hit (which must NOT fire — respx would error on the unmocked
    call).
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    _mock_all_platforms_404(evm_address.lower())
    # NB: no ``/coins/solana/contract/...`` route is mounted. If the
    # dispatcher ever routes an EVM address through the Solana path,
    # respx raises on the unmocked call and this test fails loudly.

    out = await _adapter().search_assets(evm_address)
    assert out == [], (
        "Stub fan-out returns 404 across all EVM chains; result must "
        "be an empty picker, NOT a Solana misroute"
    )


@respx.mock
async def test_search_assets_routes_solana_address_to_solana_platform() -> None:
    """A base58 Solana address dispatches to ``/coins/solana/contract``.

    Pins the Solana-only fan-out shape: exactly ONE contract call
    against the ``solana`` platform, no probes against any EVM
    chain. The Solana address shape isn't valid on any other
    CoinGecko platform, so there's nothing else to try.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    solana_route = respx.get(
        f"{_BASE_URL}/coins/solana/contract/{_FAKE_SOLANA_ADDR}",
    ).mock(
        return_value=httpx.Response(
            200,
            json=_contract_payload(
                coingecko_id="long-tail-solana-token",
                symbol="lts",
                name="Long Tail Solana",
                price_usd=0.0123,
                market_cap_usd=450_000,
            ),
        )
    )

    out = await _adapter().search_assets(_FAKE_SOLANA_ADDR)

    assert [c.coingecko_id for c in out] == ["long-tail-solana-token"]
    assert out[0].symbol == "LTS"
    assert out[0].current_price_usd == pytest.approx(0.0123)
    assert solana_route.call_count == 1, (
        "Solana dispatch must fire exactly one contract call against the solana platform"
    )


@respx.mock
async def test_search_assets_solana_dispatch_skips_evm_fan_out() -> None:
    """Solana dispatch must NOT touch any EVM contract endpoint.

    Regression guard: an earlier refactor accidentally fanned the
    Solana path out across every platform tuple. Fix kept the two
    dispatches isolated; this test pins that by mounting EVM-chain
    routes that respx would raise on if hit.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    # The Solana platform endpoint returns a 404 so the whole call
    # surfaces an empty picker — but the EVM routes must NEVER fire,
    # regardless of whether Solana resolved.
    respx.get(f"{_BASE_URL}/coins/solana/contract/{_FAKE_SOLANA_ADDR}").mock(
        return_value=httpx.Response(404, json={"error": "coin not found"})
    )

    out = await _adapter().search_assets(_FAKE_SOLANA_ADDR)
    assert out == []
    # No EVM route was mocked; respx would have errored on any such
    # call. The empty result is itself the assertion that no EVM
    # platforms were probed.


@respx.mock
async def test_search_assets_evm_fan_out_excludes_polygon_pos() -> None:
    """Polygon-pos was dropped from the EVM fan-out tuple.

    Regression guard: this test mounts the four current EVM
    platforms with 404 responses and confirms the polygon-pos
    route is never called (it'd otherwise raise unmocked under
    respx). Catches accidental reintroduction.
    """
    respx.get(f"{_BASE_URL}/search").mock(return_value=httpx.Response(200, json={"coins": []}))
    polygon_route = respx.get(
        f"{_BASE_URL}/coins/polygon-pos/contract/{_FAKE_ADDR}",
    ).mock(return_value=httpx.Response(404))
    _mock_all_platforms_404(_FAKE_ADDR)

    await _adapter().search_assets(_FAKE_ADDR)
    assert polygon_route.call_count == 0, (
        "polygon-pos must NOT be in the EVM fan-out — was dropped in favour of Solana support"
    )
