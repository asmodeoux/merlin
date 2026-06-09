"""Unit tests for the asset-picker step.

Pins the user-visible disambiguation contract:

1. CoinGecko URL parser accepts every shape we promise (with /
   without scheme, optional ``www.``, any locale, no locale) and
   rejects everything else.
2. The picker body is MarkdownV2 with one numbered row per candidate
   — symbol-as-link, price, and market cap — so look-alike rows
   (multiple "STABLE — Stable" coins) are visually distinguishable.
3. Mid-pick text routes correctly:
   * URL-shaped + resolves cleanly → exit to preview.
   * URL-shaped + parse / resolve fails → format-hint reply, stay
     in ``PICK_ASSET``. No silent fall-through to a text search.
   * Plain text → fresh search re-renders the picker.

PTB plumbing is mocked the same shape as the other ``step_*`` tests
(``test_alpha_step_horizon``, ``test_alpha_freeform_autopick``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.alpha import step_asset
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    AlphaState,
)
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardMarkup, Message

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _fake_message(*, chat_id: int = 555, message_id: int = 1) -> MagicMock:
    """Mock that passes ``isinstance(_, telegram.Message)`` for ``ui.render``."""
    m = MagicMock(spec=Message)
    m.chat_id = chat_id
    m.message_id = message_id
    return m


def _text_update(*, chat_id: int = 555, text: str = "stable") -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.effective_chat = MagicMock(id=chat_id)
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update.message = msg
    return update


def _context(
    *,
    client: MagicMock | None = None,
    user_data: dict[str, Any] | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {} if user_data is None else user_data
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=_fake_message())
    ctx.bot.edit_message_reply_markup = AsyncMock()
    ctx.bot_data = {BOT_DATA_KEY: client} if client is not None else {BOT_DATA_KEY: MagicMock()}
    return ctx


def _stable_candidates() -> list[dict[str, Any]]:
    """The live `STABLE x3` look-alike scenario from the user's screenshot."""
    return [
        {
            "coingecko_id": "stable",
            "symbol": "STABLE",
            "name": "Stable",
            "market_cap_rank": 800,
            "current_price_usd": 0.87,
            "market_cap_usd": 5_200_000,
        },
        {
            "coingecko_id": "stable-2",
            "symbol": "STABLE",
            "name": "Stable",
            "market_cap_rank": 1500,
            "current_price_usd": 0.21,
            "market_cap_usd": 200_000,
        },
        {
            "coingecko_id": "satoshi-stablecoin",
            "symbol": "SATUSD",
            "name": "Satoshi Stablecoin",
            "market_cap_rank": 900,
            "current_price_usd": 0.99,
            "market_cap_usd": 2_000_000,
        },
    ]


# ---------------------------------------------------------------------------
# _parse_coingecko_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.coingecko.com/en/coins/stable-2", "stable-2"),
        ("http://www.coingecko.com/en/coins/bitcoin", "bitcoin"),
        ("https://coingecko.com/en/coins/ethereum", "ethereum"),
        # No scheme — accepted via implicit ``https://`` prefix.
        ("www.coingecko.com/coins/bitcoin", "bitcoin"),
        ("coingecko.com/en/coins/ethereum", "ethereum"),
        # Non-English locales.
        ("https://www.coingecko.com/ru/coins/the-open-network", "the-open-network"),
        ("https://www.coingecko.com/zh-tw/coins/solana", "solana"),
        # No locale segment at all.
        ("https://www.coingecko.com/coins/cardano", "cardano"),
        # Trailing slash / query / fragment ignored.
        ("https://www.coingecko.com/en/coins/stable-2/", "stable-2"),
        ("https://www.coingecko.com/en/coins/stable-2?utm=x", "stable-2"),
        ("https://www.coingecko.com/en/coins/stable-2#chart", "stable-2"),
        # Surrounding whitespace.
        ("   https://www.coingecko.com/en/coins/stable-2   ", "stable-2"),
        # Mixed case in the host is fine; slug is normalised to lower.
        ("HTTPS://WWW.CoinGecko.COM/EN/coins/stable-2", "stable-2"),
    ],
)
def test_parse_coingecko_url_happy_paths(url: str, expected: str) -> None:
    assert step_asset._parse_coingecko_url(url) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Plain ticker — must NOT look like a URL.
        "STABLE",
        "stable",
        "Bitcoin",
        "ETH long 2 weeks",
        # Right shape, wrong host.
        "https://example.com/coins/bitcoin",
        "https://coinmarketcap.com/coins/bitcoin",
        # Right host, wrong section.
        "https://www.coingecko.com/en/exchanges/binance",
        "https://www.coingecko.com/en/categories/meme",
        # Coin path with no slug.
        "https://www.coingecko.com/en/coins/",
        "https://www.coingecko.com/coins",
        # Slug with disallowed characters.
        "https://www.coingecko.com/en/coins/bitcoin!evil",
        "https://www.coingecko.com/en/coins/bitcoin@",
        # Empty / whitespace-only.
        "",
        "   ",
        # Looks like a URL but on a different domain that includes "coingecko" as a substring.
        "https://notcoingecko.com.evil.com/en/coins/bitcoin",
    ],
)
def test_parse_coingecko_url_rejects(text: str) -> None:
    assert step_asset._parse_coingecko_url(text) is None


# ---------------------------------------------------------------------------
# _format_money / _format_market_cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "—"),
        ("not-a-number", "—"),
        (0, "$0.00"),
        (0.87, "$0.87"),
        (0.21, "$0.21"),
        (0.99, "$0.99"),
        (1.0, "$1.00"),
        (1234.5, "$1,234.50"),
        (64500.12, "$64,500.12"),
        # Sub-cent micro-cap: 4-decimal format for $0.01..$1.00 range.
        (0.0094, "$0.0094"),
        # Sub-cent below $0.01: 6-significant-digit format.
        (0.000123, "$0.000123"),
    ],
)
def test_format_money_renders_each_band(value: object, expected: str) -> None:
    assert step_asset._format_money(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "—"),
        ("not-a-number", "—"),
        (0, "—"),
        (-1, "—"),
        (500, "$500"),
        (200_000, "$200k"),
        (5_200_000, "$5.2M"),
        (2_000_000, "$2.0M"),
        (1_270_000_000_000, "$1270.0B"),
        (1_200_000_000, "$1.2B"),
    ],
)
def test_format_market_cap_abbreviates_each_tier(value: object, expected: str) -> None:
    assert step_asset._format_market_cap(value) == expected


# ---------------------------------------------------------------------------
# _render_candidates_text
# ---------------------------------------------------------------------------


def test_render_candidates_text_has_one_numbered_line_per_candidate() -> None:
    """Body has the header, one row per candidate with the matching
    number emoji, the linked symbol, the name, the price, and the
    market cap. Footer advertises the URL / search / button options.
    """
    body = step_asset._render_candidates_text(_stable_candidates())
    lines = body.splitlines()
    assert lines[0] == "Which one did you mean?"
    # Three candidates → three numbered rows. Number emojis line up
    # with ``_NUMBER_EMOJI[i]`` for i in 0..2.
    for i, expected_emoji in enumerate(step_asset._NUMBER_EMOJI[:3]):
        assert lines[1 + i].startswith(expected_emoji), (
            f"row {i + 1} must start with the matching keycap emoji "
            f"{expected_emoji!r}; got {lines[1 + i]!r}"
        )
    # Each row carries the linked symbol pointing at the CoinGecko URL.
    assert "[STABLE](https://www.coingecko.com/en/coins/stable)" in lines[1]
    assert "[STABLE](https://www.coingecko.com/en/coins/stable-2)" in lines[2]
    assert "[SATUSD](https://www.coingecko.com/en/coins/satoshi-stablecoin)" in lines[3]
    # Prices + market caps land on the body (MarkdownV2-escaped: ``$`` is not reserved).
    assert "$0\\.87" in lines[1]
    assert "$5\\.2M cap" in lines[1]
    assert "$0\\.21" in lines[2]
    assert "$200k cap" in lines[2]
    # Footer advertises all three input affordances.
    footer = lines[-1]
    assert "Tap" in footer
    assert "ticker" in footer
    assert "URL" in footer


def test_render_candidates_text_escapes_markdown_v2_reserved_in_name() -> None:
    """Names like "C+Charge" or "X.AI" contain MarkdownV2-reserved
    chars. The renderer must escape them so Telegram doesn't error
    out with ``Bad Request: can't parse entities``."""
    candidates = [
        {
            "coingecko_id": "c-charge",
            "symbol": "CCC",
            "name": "C+Charge (Beta)",
            "current_price_usd": 1.0,
            "market_cap_usd": 10_000,
        },
    ]
    body = step_asset._render_candidates_text(candidates)
    # ``+`` ``(`` ``)`` are all MarkdownV2-reserved.
    assert r"C\+Charge \(Beta\)" in body


def test_render_candidates_text_renders_dash_for_missing_market_data() -> None:
    """Long-tail rows without market data must still render — ``—``
    in the price / cap slots, no crash."""
    candidates = [
        {
            "coingecko_id": "halted-coin",
            "symbol": "HLT",
            "name": "Halted",
            "current_price_usd": None,
            "market_cap_usd": None,
        },
    ]
    body = step_asset._render_candidates_text(candidates)
    assert "—" in body


# ---------------------------------------------------------------------------
# _candidates_keyboard
# ---------------------------------------------------------------------------


def test_candidates_keyboard_buttons_have_numbered_prefix_and_callback() -> None:
    """Buttons get the same numbered prefix as the body, and the
    callback_data carries the ``alpha:asset:<coingecko_id>`` shape
    expected by ``on_pick``.
    """
    keyboard = step_asset._candidates_keyboard(_stable_candidates())
    assert isinstance(keyboard, InlineKeyboardMarkup)
    rows = keyboard.inline_keyboard
    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert len(row) == 1
        btn = row[0]
        assert btn.text.startswith(step_asset._NUMBER_EMOJI[i])
        cb = btn.callback_data
        assert isinstance(cb, str)
        assert cb.startswith(step_asset.CB_PREFIX)
    # Distinct coingecko_ids land in the callback so look-alike rows
    # still route to the right resolve call.
    callbacks = [row[0].callback_data for row in rows]
    assert callbacks == [
        f"{step_asset.CB_PREFIX}stable",
        f"{step_asset.CB_PREFIX}stable-2",
        f"{step_asset.CB_PREFIX}satoshi-stablecoin",
    ]


# ---------------------------------------------------------------------------
# on_text_during_pick — URL flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_text_during_pick_url_success_routes_to_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful URL → ``client.resolve_asset`` → preview. No interim
    message; the preview itself is the user's confirmation that the
    pick stuck."""
    client = MagicMock()
    client.resolve_asset = AsyncMock(
        return_value={
            "asset_id": "00000000-0000-0000-0000-000000000001",
            "coingecko_id": "stable-2",
            "symbol": "STABLE",
            "name": "Stable",
        }
    )
    update = _text_update(text="https://www.coingecko.com/en/coins/stable-2")
    ctx = _context(client=client)

    preview_calls: list[tuple[Any, Any]] = []

    async def _fake_show_preview(u: Any, c: Any) -> int:
        preview_calls.append((u, c))
        return AlphaState.PREVIEW

    monkeypatch.setattr(step_asset, "show_preview", _fake_show_preview)

    result = await step_asset.on_text_during_pick(update, ctx)

    client.resolve_asset.assert_awaited_once_with(coingecko_id="stable-2")
    assert ctx.user_data[USER_KEY_ASSET_RESOLUTION]["coingecko_id"] == "stable-2"
    assert len(preview_calls) == 1
    assert result == AlphaState.PREVIEW
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_on_text_during_pick_url_parse_failure_emits_format_hint() -> None:
    """A URL-shaped reply that doesn't parse to a slug must produce
    the format hint and stay in PICK_ASSET. No fall-through to a
    text search — the user tried a URL, so they get a URL-shaped
    explanation."""
    client = MagicMock()
    client.resolve_asset = AsyncMock()
    client.search_assets = AsyncMock()
    update = _text_update(text="https://www.coingecko.com/en/exchanges/binance")
    ctx = _context(client=client)

    result = await step_asset.on_text_during_pick(update, ctx)

    update.message.reply_text.assert_awaited_once()
    hint = update.message.reply_text.call_args.args[0]
    assert "couldn't read that as a CoinGecko token link" in hint.lower() or (
        "expected one of these formats" in hint.lower()
    )
    # Three example URLs in the hint so the user can copy-paste a fix.
    assert hint.count("coingecko.com") >= 3
    # No search / resolve was attempted — the URL flow never silently
    # falls through to text search.
    client.resolve_asset.assert_not_called()
    client.search_assets.assert_not_called()
    assert result == AlphaState.PICK_ASSET


@pytest.mark.asyncio
async def test_on_text_during_pick_url_resolve_404_emits_format_hint() -> None:
    """Slug parsed cleanly but ``client.resolve_asset`` 404s → same
    format-hint reply, same PICK_ASSET state. The user gets a
    consistent failure surface regardless of whether parse or resolve
    is the culprit."""
    client = MagicMock()
    client.resolve_asset = AsyncMock(
        side_effect=MerlinHTTPError(404, "Not Found", detail={"detail": "unknown id"})
    )
    update = _text_update(text="coingecko.com/en/coins/totally-made-up")
    ctx = _context(client=client)

    result = await step_asset.on_text_during_pick(update, ctx)

    client.resolve_asset.assert_awaited_once_with(coingecko_id="totally-made-up")
    update.message.reply_text.assert_awaited_once()
    assert result == AlphaState.PICK_ASSET
    # Nothing landed on user_data — we never reached the success branch.
    assert USER_KEY_ASSET_RESOLUTION not in ctx.user_data


# ---------------------------------------------------------------------------
# on_text_during_pick — fresh search flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_text_during_pick_plain_text_triggers_new_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain ticker mid-pick refreshes the candidate list. The
    picker keyboard sails out as a new message below the existing
    one (mirrors ``receive_horizon_text``: text reply has no
    callback_query, so ``ui_render`` sends a fresh message)."""
    new_candidates = _stable_candidates()
    client = MagicMock()
    client.search_assets = AsyncMock(return_value={"candidates": new_candidates})
    client.resolve_asset = AsyncMock()
    update = _text_update(text="stable")
    ctx = _context(
        client=client,
        # Pre-existing different candidates to verify the slot is overwritten.
        user_data={USER_KEY_CANDIDATES: [{"coingecko_id": "old", "symbol": "OLD", "name": "Old"}]},
    )

    rendered: list[tuple[Any, ...]] = []

    async def _fake_render(*args: Any, **kwargs: Any) -> Any:
        rendered.append((args, kwargs))
        return _fake_message()

    monkeypatch.setattr(step_asset, "ui_render", _fake_render)

    result = await step_asset.on_text_during_pick(update, ctx)

    client.search_assets.assert_awaited_once_with(query="stable")
    # New candidates landed; old ones gone.
    assert ctx.user_data[USER_KEY_CANDIDATES] == new_candidates
    # ``_render_picker`` called ``ui_render`` once with MarkdownV2 + link
    # preview suppression — the picker shape.
    assert len(rendered) == 1
    kwargs = rendered[0][1]
    assert kwargs.get("parse_mode") == "MarkdownV2"
    assert kwargs.get("disable_link_preview") is True
    assert isinstance(kwargs.get("reply_markup"), InlineKeyboardMarkup)
    # Body carries the new candidates' symbols.
    body_text = kwargs.get("text", "")
    assert "STABLE" in body_text
    assert "SATUSD" in body_text
    # We stay on the picker.
    assert result == AlphaState.PICK_ASSET
    client.resolve_asset.assert_not_called()


@pytest.mark.asyncio
async def test_on_text_during_pick_plain_text_empty_results_warns_user() -> None:
    """If the new search returns zero candidates, the user gets the
    'No matches' message — the picker doesn't render an empty list.
    """
    client = MagicMock()
    client.search_assets = AsyncMock(return_value={"candidates": []})
    update = _text_update(text="zzzzz-no-such-coin")
    ctx = _context(client=client, user_data={USER_KEY_CANDIDATES: _stable_candidates()})

    result = await step_asset.on_text_during_pick(update, ctx)

    update.message.reply_text.assert_awaited_once()
    assert "No matches" in update.message.reply_text.call_args.args[0]
    # State falls back to EDIT_ASSET since ``receive_asset_query`` is
    # the underlying handler — matches its existing contract.
    assert result == AlphaState.EDIT_ASSET
