"""Asset picker step — search-driven (Q-027 lazy resolve).

User types a query (e.g. "ether"), bot calls ``GET /assets/search``,
renders the top-5 candidates as buttons, and on pick calls
``POST /assets/resolve`` to materialise the local row.
"""

from __future__ import annotations

import logging
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview

_log = logging.getLogger("merlin.bot.handlers.alpha.asset")

CB_PREFIX = "alpha:asset:"


async def prompt_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user which asset they mean."""
    query = update.callback_query
    if query is not None:
        await query.answer()
    if update.effective_chat is None:
        return ConversationHandler.END

    candidates = context.user_data.get(USER_KEY_CANDIDATES, []) if context.user_data else []
    if candidates:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Which one did you mean?",
            reply_markup=_candidates_keyboard(candidates),
        )
        return AlphaState.PICK_ASSET

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Which asset? Type a name or ticker (BTC, ETH, RNDR, …).",
    )
    return AlphaState.EDIT_ASSET


async def receive_asset_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a search query; hit the API and render candidates."""
    if update.message is None or update.effective_chat is None:
        return ConversationHandler.END
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("Type the asset's name or ticker.")
        return AlphaState.EDIT_ASSET

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.search_assets(query=query)
    except MerlinHTTPError as e:
        _log.warning("search_assets failed: %s", e)
        await update.message.reply_text("Couldn't reach market data — try again in a moment.")
        return AlphaState.EDIT_ASSET

    candidates = result.get("candidates", [])
    if not candidates:
        await update.message.reply_text("No matches. Try a ticker like BTC or ETH, or a coin name.")
        return AlphaState.EDIT_ASSET

    if context.user_data is not None:
        context.user_data[USER_KEY_CANDIDATES] = candidates

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Which one did you mean?",
        reply_markup=_candidates_keyboard(candidates),
    )
    return AlphaState.PICK_ASSET


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked a candidate. Resolve it via the API; back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None or update.effective_chat is None:
        return ConversationHandler.END
    await query.answer()
    coingecko_id = (query.data or "").removeprefix(CB_PREFIX)
    if not coingecko_id:
        return AlphaState.PICK_ASSET

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        resolution = await client.resolve_asset(coingecko_id=coingecko_id)
    except MerlinHTTPError as e:
        _log.warning("resolve_asset failed: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Couldn't lock that in — try picking again.",
        )
        return AlphaState.PICK_ASSET

    context.user_data[USER_KEY_ASSET_RESOLUTION] = resolution
    return await show_preview(update, context)


def _candidates_keyboard(candidates: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Render up to 5 candidates as one button per row."""
    rows: list[list[InlineKeyboardButton]] = []
    for c in candidates[:5]:
        label = f"{c.get('symbol', '?')} — {c.get('name', '?')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_PREFIX}{c['coingecko_id']}")])
    return InlineKeyboardMarkup(rows)
