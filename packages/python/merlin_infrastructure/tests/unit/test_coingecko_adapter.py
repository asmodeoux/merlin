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
