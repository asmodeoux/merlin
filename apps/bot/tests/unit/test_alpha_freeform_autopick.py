"""Freeform auto-asset-pick regression suite.

Sprint-1 live bug: the LLM returned ``asset_query="ETH"`` and the API
returned 5 ranked candidates with Ethereum as the top hit, but the bot
stored only the candidate list — never selected one. The preview showed
``Asset: (tap ✏️ Edit Asset)`` for an unambiguous "ETH" call. These
tests pin the contract:

* On a clean symbol / name match between the LLM's ``asset_query`` and
  the top CoinGecko candidate, the bot auto-fills the asset.
* On ambiguity (different symbol, no candidates, blank asset_query),
  the bot stays out of the way — the user gets the picker.
* The auto-pick is byte-for-byte compatible with what the asset
  resolve step writes, so downstream code (preview render + submit
  payload) cannot tell the difference.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.alpha import step_freeform
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    USER_KEY_DRAFT,
)

# ---------------------------------------------------------------------------
# Pure _auto_pick heuristic
# ---------------------------------------------------------------------------


_ETH_CANDIDATES = [
    {"coingecko_id": "ethereum", "symbol": "ETH", "name": "Ethereum", "market_cap_rank": 2},
    {"coingecko_id": "tether", "symbol": "USDT", "name": "Tether", "market_cap_rank": 3},
]


def test_auto_pick_returns_top_candidate_on_exact_symbol_match() -> None:
    """Q="ETH", top symbol="ETH" → auto-pick Ethereum."""
    pick = step_freeform._auto_pick("ETH", _ETH_CANDIDATES)
    assert pick is not None
    assert pick["coingecko_id"] == "ethereum"


def test_auto_pick_is_case_insensitive() -> None:
    """Q="eth" / "Eth" / "ETH" all match the top symbol "ETH"."""
    for q in ("eth", "Eth", "ETH", "EtH"):
        pick = step_freeform._auto_pick(q, _ETH_CANDIDATES)
        assert pick is not None, f"failed to auto-pick on q={q!r}"


def test_auto_pick_matches_full_name() -> None:
    """Q="bitcoin", top name="Bitcoin" → auto-pick (user typed the
    full name; the LLM passed it through verbatim)."""
    candidates = [
        {"coingecko_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin", "market_cap_rank": 1},
    ]
    pick = step_freeform._auto_pick("bitcoin", candidates)
    assert pick is not None
    assert pick["coingecko_id"] == "bitcoin"


def test_auto_pick_prefers_exact_symbol_match_over_top_substring_hit() -> None:
    """Live Sprint-1 bug: q="TON" but CoinGecko's top hit is "Canton"
    (rank 21, symbol=CC) because its *name* substring-matches "ton";
    Toncoin (rank 23, symbol=TON) sits at #2. The previous top-only
    check returned None and left the user to open Edit Asset manually.
    An exact symbol match anywhere in the list must beat a substring
    hit at the top — Toncoin is the only coin whose ticker actually
    is "TON".
    """
    candidates = [
        {"coingecko_id": "canton-network", "symbol": "CC", "name": "Canton", "market_cap_rank": 21},
        {
            "coingecko_id": "the-open-network",
            "symbol": "TON",
            "name": "Toncoin",
            "market_cap_rank": 23,
        },
        {
            "coingecko_id": "tokamak-network",
            "symbol": "TON",
            "name": "Tokamak Network",
            "market_cap_rank": 701,
        },
    ]
    pick = step_freeform._auto_pick("TON", candidates)
    assert pick is not None
    assert pick["coingecko_id"] == "the-open-network", (
        "exact symbol match must win over a higher-ranked substring hit, "
        "and ties on symbol break by CoinGecko's market-cap-rank order"
    )


def test_auto_pick_returns_none_when_no_candidate_matches() -> None:
    """Truly ambiguous: no candidate's symbol or name equals the query.
    The user gets the picker (no false-positive auto-pick).
    """
    candidates = [
        {
            "coingecko_id": "ethena-usde",
            "symbol": "USDE",
            "name": "Ethena USDe",
            "market_cap_rank": 25,
        },
        {
            "coingecko_id": "ethereum-name-service",
            "symbol": "ENS",
            "name": "Ethereum Name Service",
            "market_cap_rank": 60,
        },
    ]
    assert step_freeform._auto_pick("ETH", candidates) is None


def test_auto_pick_returns_none_with_no_candidates() -> None:
    assert step_freeform._auto_pick("ETH", []) is None


def test_auto_pick_returns_none_with_no_asset_query() -> None:
    assert step_freeform._auto_pick(None, _ETH_CANDIDATES) is None
    assert step_freeform._auto_pick("", _ETH_CANDIDATES) is None
    assert step_freeform._auto_pick("   ", _ETH_CANDIDATES) is None


# ---------------------------------------------------------------------------
# _handle_text wiring — the live bug
# ---------------------------------------------------------------------------


def _draft_response(
    *,
    asset_query: str | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "asset_query": asset_query,
        "asset_candidates": candidates,
        "direction": "long",
        "horizon_seconds": 1_209_600,
        "conviction": "strong",
        "thesis_text": "Vitalik staking",
        "extracted_fields": {"schema_version": 1},
        "parse_metadata": {
            "parsed_via": "freeform_llm",
            "model": "openai/gpt-5-mini",
            "raw_user_turns": ["ETH long 14d strong"],
            "confidences": {"asset_query": 0.99},
        },
    }


def _text_update(*, chat_id: int = 555, message_id: int = 1234) -> MagicMock:
    msg = MagicMock()
    msg.text = "ETH long 2 weeks"
    msg.chat_id = chat_id
    msg.message_id = message_id
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.callback_query = None
    update.effective_chat = MagicMock(id=chat_id)
    return update


def _context(*, client: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock())
    # The freeform handler fires a "Thinking…" draft via ``bot._post``
    # (Bot API 10.1) before the LLM call; the real ExtBot has ``_post``,
    # so the mock must expose an awaitable one too.
    ctx.bot._post = AsyncMock()
    ctx.bot_data = {BOT_DATA_KEY: client}
    return ctx


@pytest.mark.asyncio
async def test_handle_text_autofills_asset_resolution_on_clean_match() -> None:
    """Live bug: API returned ETH candidates, bot stored them, preview
    still showed ``Asset: (tap ✏️ Edit Asset)``. Auto-pick must write
    a USER_KEY_ASSET_RESOLUTION when there's a clear top hit."""
    client = MagicMock()
    client.draft_claim = AsyncMock(
        return_value=_draft_response(asset_query="ETH", candidates=_ETH_CANDIDATES)
    )
    update = _text_update()
    ctx = _context(client=client)

    await step_freeform._handle_text(update, ctx, "ETH long 2 weeks")

    assert USER_KEY_ASSET_RESOLUTION in ctx.user_data, (
        "auto-pick failed to write USER_KEY_ASSET_RESOLUTION for an "
        "unambiguous ETH → Ethereum match"
    )
    resolution = ctx.user_data[USER_KEY_ASSET_RESOLUTION]
    assert resolution["coingecko_id"] == "ethereum"
    assert resolution["symbol"] == "ETH"
    assert resolution["name"] == "Ethereum"


@pytest.mark.asyncio
async def test_handle_text_skips_autofill_on_ambiguous_match() -> None:
    """When the top candidate doesn't match the asset_query, leave
    USER_KEY_ASSET_RESOLUTION unset — the user picks via Edit Asset."""
    client = MagicMock()
    ambiguous_candidates = [
        {
            "coingecko_id": "ethena-usde",
            "symbol": "USDE",
            "name": "Ethena USDe",
            "market_cap_rank": 25,
        }
    ]
    client.draft_claim = AsyncMock(
        return_value=_draft_response(asset_query="ETH", candidates=ambiguous_candidates)
    )
    update = _text_update()
    ctx = _context(client=client)

    await step_freeform._handle_text(update, ctx, "Some weird sentence about ETH")

    assert USER_KEY_ASSET_RESOLUTION not in ctx.user_data
    # But candidates were stored so the picker has something to show.
    assert ctx.user_data[USER_KEY_CANDIDATES] == ambiguous_candidates


@pytest.mark.asyncio
async def test_handle_text_shows_thinking_bubble_before_llm_call() -> None:
    """Before the (several-second) LLM extraction, the handler streams a
    "Thinking…" draft (Bot API 10.1 ``sendMessageDraft`` with non-empty
    text) targeting the caller's chat, keyed by the triggering message
    id."""
    client = MagicMock()
    client.draft_claim = AsyncMock(
        return_value=_draft_response(asset_query="ETH", candidates=_ETH_CANDIDATES)
    )
    update = _text_update(chat_id=555, message_id=1234)
    ctx = _context(client=client)

    await step_freeform._handle_text(update, ctx, "ETH long 2 weeks")

    ctx.bot._post.assert_awaited_once_with(
        "sendMessageDraft",
        {"chat_id": 555, "draft_id": 1234, "text": "Thinking…"},
    )


@pytest.mark.asyncio
async def test_handle_text_survives_thinking_bubble_failure() -> None:
    """The thinking bubble is cosmetic + best-effort: a Telegram-side
    failure (older API server, etc.) must NOT abort the draft flow."""
    from telegram.error import TelegramError

    client = MagicMock()
    client.draft_claim = AsyncMock(
        return_value=_draft_response(asset_query="ETH", candidates=_ETH_CANDIDATES)
    )
    update = _text_update()
    ctx = _context(client=client)
    ctx.bot._post = AsyncMock(side_effect=TelegramError("drafts unsupported"))

    await step_freeform._handle_text(update, ctx, "ETH long 2 weeks")

    # Draft flow still completed despite the bubble failing.
    client.draft_claim.assert_awaited_once()
    assert USER_KEY_DRAFT in ctx.user_data


@pytest.mark.asyncio
async def test_handle_text_still_stores_draft_and_candidates() -> None:
    """Auto-pick is a layer on top of the existing storage — the
    draft and candidates must still land in user_data either way."""
    client = MagicMock()
    client.draft_claim = AsyncMock(
        return_value=_draft_response(asset_query="ETH", candidates=_ETH_CANDIDATES)
    )
    update = _text_update()
    ctx = _context(client=client)

    await step_freeform._handle_text(update, ctx, "ETH up 2 weeks")

    assert USER_KEY_DRAFT in ctx.user_data
    assert ctx.user_data[USER_KEY_DRAFT]["direction"] == "long"
    assert ctx.user_data[USER_KEY_CANDIDATES] == _ETH_CANDIDATES
