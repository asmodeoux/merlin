"""Entry-point step for ``/alpha`` — calls ``POST /claims/draft``.

Handles two cases:

- User invoked ``/alpha`` with no text → prompt for input.
- User typed a sentence (either after ``/alpha`` or as a plain message
  caught by the global text handler) → send to the LLM extractor.

On LLM failure, transparently falls through to the asset step (the
button flow's natural starting point). The user sees no "fallback"
message — the bot just asks "Which asset?" as if that were the
intended first step. The plan's principle: LLM is UX acceleration,
never a system dependency.
"""

from __future__ import annotations

import logging

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview

_log = logging.getLogger("merlin.bot.handlers.alpha.freeform")


async def alpha_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/alpha`` invoked. Treat any trailing args as initial freeform text."""
    if update.message is None or update.effective_user is None:
        return ConversationHandler.END

    args_text = " ".join(context.args or []).strip()
    if args_text:
        return await _handle_text(update, context, args_text)

    await update.message.reply_text("Describe what you think will happen — or hit /cancel to bail.")
    return AlphaState.AWAIT_TEXT


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent us their freeform sentence."""
    if update.message is None:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Describe the move in your own words.")
        return AlphaState.AWAIT_TEXT
    return await _handle_text(update, context, text)


async def _handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> int:
    """Hit the API's draft endpoint and route to preview or the asset picker."""
    if update.message is None:
        return ConversationHandler.END

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        draft = await client.draft_claim(text=text)
    except MerlinHTTPError as e:
        _log.warning("claim draft failed (status=%s); falling back to form", e.status_code)
        return await _enter_form_flow(update, context, raw_text=text)
    except Exception as e:
        _log.warning("claim draft transport error: %s; falling back to form", e)
        return await _enter_form_flow(update, context, raw_text=text)

    if context.user_data is not None:
        context.user_data[USER_KEY_DRAFT] = draft
        candidates = draft.get("asset_candidates", [])
        context.user_data[USER_KEY_CANDIDATES] = candidates
        # If the LLM was confident about the asset AND CoinGecko's top
        # candidate cleanly matches that query, auto-pick it. Otherwise
        # the user has to manually click "Edit Asset" → pick the
        # obvious top hit, which is a needless ceremony when "ETH"
        # unambiguously means Ethereum. Anything ambiguous (symbol
        # mismatch, no clear winner) falls through to the Edit Asset
        # picker as before.
        auto = _auto_pick(draft.get("asset_query"), candidates)
        if auto is not None:
            context.user_data[USER_KEY_ASSET_RESOLUTION] = {
                "coingecko_id": auto["coingecko_id"],
                "symbol": auto["symbol"],
                "name": auto["name"],
            }

    return await show_preview(update, context)


def _auto_pick(
    asset_query: str | None,
    candidates: list[dict[str, object]],
) -> dict[str, object] | None:
    """Return the top candidate iff it's clearly the LLM's intended asset.

    Conservative heuristic: the top candidate's ``symbol`` or ``name``
    matches the LLM's ``asset_query`` case-insensitively. This catches
    the common case ("ETH" → Ethereum ETH, "BTC" → Bitcoin BTC,
    "bitcoin" → Bitcoin) without auto-locking on ambiguity ("eth" in a
    sentence about Ethena would still trigger the picker because the
    top hit's symbol is "USDE" — no match).

    On Confirm, ``POST /claims`` still calls CoinGecko ``get_asset_metadata``
    server-side as a defence against forged ids, so an auto-pick can
    never push an invalid claim through. The user can also override
    via the Edit Asset button at any point before Confirm.
    """
    if not asset_query or not candidates:
        return None
    q = asset_query.strip().lower()
    top = candidates[0]
    symbol = str(top.get("symbol", "")).lower()
    name = str(top.get("name", "")).lower()
    if q in {symbol, name}:
        return top
    return None


async def _enter_form_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    raw_text: str,
) -> int:
    """Drop into the deterministic form flow starting at the asset step."""
    if context.user_data is not None:
        # Empty draft skeleton so the preview step renders the form path.
        context.user_data[USER_KEY_DRAFT] = {
            "direction": None,
            "horizon": None,
            "conviction": None,
            "thesis_text": raw_text,
            "asset_query": None,
            "asset_candidates": [],
            "extracted_fields": {},
            "parse_metadata": {
                "parsed_via": "form",
                "raw_user_turns": [raw_text] if raw_text else [],
            },
        }
        context.user_data[USER_KEY_CANDIDATES] = []
    # Sprint 1: hand off to step_asset's "Which asset?" prompt directly.
    # For now, the preview step renders a "What's the asset?" sub-prompt
    # which is enough to keep the conversation alive.
    return await show_preview(update, context)
