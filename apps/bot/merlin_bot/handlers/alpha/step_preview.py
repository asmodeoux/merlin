"""Preview step — central hub of the ``/alpha`` flow.

Renders the prefilled fields as a single message + Confirm / Edit /
Cancel inline keyboard. Per the bot-voice rules (plan §6): no labels-
with-colons, no internal nouns, no $-mapped conviction size, no
target-price echoes beyond what the user's own thesis already says.

The "silent capture" rule: anything in ``extracted_fields`` that the
v0 verifier can't score is **not surfaced**. It lives in the DB for
the record; the user's verbatim thesis is the attestation.
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
    USER_KEY_DRAFT,
    AlphaState,
)

_log = logging.getLogger("merlin.bot.handlers.alpha.preview")

# Callback data prefixes. Short to fit in TG's 64-byte limit.
CB_CONFIRM = "alpha:confirm"
CB_CANCEL = "alpha:cancel"
CB_EDIT_ASSET = "alpha:edit:asset"
CB_EDIT_DIRECTION = "alpha:edit:direction"
CB_EDIT_HORIZON = "alpha:edit:horizon"
CB_EDIT_CONVICTION = "alpha:edit:conviction"
CB_EDIT_THESIS = "alpha:edit:thesis"


async def show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Render the preview message + inline keyboard. Always returns PREVIEW."""
    if update.effective_chat is None or context.user_data is None:
        return ConversationHandler.END

    draft = context.user_data.get(USER_KEY_DRAFT, {})
    asset = context.user_data.get(USER_KEY_ASSET_RESOLUTION, {})

    text = _render_preview(draft, asset)
    keyboard = _build_preview_keyboard(draft, asset)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=keyboard,
    )
    return AlphaState.PREVIEW


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked Confirm. POST /claims and send the receipt."""
    query = update.callback_query
    if query is None or context.user_data is None or update.effective_chat is None:
        return ConversationHandler.END
    await query.answer()

    draft = context.user_data.get(USER_KEY_DRAFT, {})
    asset = context.user_data.get(USER_KEY_ASSET_RESOLUTION, {})

    payload = _build_submit_payload(draft, asset)
    if payload is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Looks like a field is still missing. Hit Edit on what's blank.",
        )
        return AlphaState.PREVIEW

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.submit_claim(payload=payload)
    except MerlinHTTPError as e:
        _log.warning("submit_claim failed: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Something's off on our end. Try /alpha again in a moment.",
        )
        return ConversationHandler.END

    asset_symbol = result.get("asset_symbol", "your call")
    resolves_at = result.get("resolves_at", "")
    receipt = _render_receipt(asset_symbol=asset_symbol, resolves_at=resolves_at)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=receipt)

    _clear(context)
    return ConversationHandler.END


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked Cancel. Drop the draft from memory."""
    query = update.callback_query
    if query is not None:
        await query.answer()
    if update.effective_chat is not None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="No problem. Hit /alpha when you've got something to call.",
        )
    _clear(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_preview(draft: dict[str, Any], asset: dict[str, Any]) -> str:
    """Compose the preview text. Bot-voice rules strictly enforced."""
    asset_symbol = asset.get("symbol") or _placeholder("asset")
    direction = _direction_human(draft.get("direction"))
    horizon = _horizon_human(draft.get("horizon"))
    conviction = _conviction_human(draft.get("conviction"))
    thesis = (draft.get("thesis_text") or "").strip() or _placeholder("thesis")

    lines = [
        "Here's your call:",
        "",
        f"{asset_symbol} · {direction} · {horizon} · {conviction}",
        "",
        f'"{thesis}"',
        "",
        "Tap Confirm if it looks right, or Edit a field.",
    ]
    return "\n".join(lines)


def _render_receipt(*, asset_symbol: str, resolves_at: str) -> str:
    """Receipt message shown after a successful submit."""
    date_human = _calendar_date(resolves_at)
    return f"Got it. Resolves {date_human}. /me to see your open calls."


def _build_preview_keyboard(
    draft: dict[str, Any],
    asset: dict[str, Any],
) -> InlineKeyboardMarkup:
    """Build the Confirm / Edit / Cancel keyboard."""
    # Confirm always shown — the on_confirm handler validates and
    # re-shows the preview if anything's missing, with a hint.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Confirm", callback_data=CB_CONFIRM)],
            [
                InlineKeyboardButton("Edit asset", callback_data=CB_EDIT_ASSET),
                InlineKeyboardButton("Edit direction", callback_data=CB_EDIT_DIRECTION),
            ],
            [
                InlineKeyboardButton("Edit horizon", callback_data=CB_EDIT_HORIZON),
                InlineKeyboardButton("Edit conviction", callback_data=CB_EDIT_CONVICTION),
            ],
            [InlineKeyboardButton("Edit thesis", callback_data=CB_EDIT_THESIS)],
            [InlineKeyboardButton("Cancel", callback_data=CB_CANCEL)],
        ]
    )


def _build_submit_payload(
    draft: dict[str, Any],
    asset: dict[str, Any],
) -> dict[str, Any] | None:
    """Compose the ``SubmitClaimRequest`` payload; None if a required field is missing."""
    if not asset or not asset.get("coingecko_id"):
        return None
    direction = draft.get("direction")
    horizon = draft.get("horizon")
    conviction = draft.get("conviction")
    thesis_text = (draft.get("thesis_text") or "").strip()
    if not (direction and horizon and conviction and thesis_text):
        return None
    return {
        "coingecko_id": asset["coingecko_id"],
        "direction": direction,
        "horizon": horizon,
        "conviction": conviction,
        "thesis_text": thesis_text,
        "extracted_fields": draft.get("extracted_fields") or {"schema_version": 1},
        "parse_metadata": draft.get("parse_metadata") or {"parsed_via": "form"},
    }


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the conversation's user_data entries."""
    if context.user_data is None:
        return
    for key in (USER_KEY_DRAFT, USER_KEY_ASSET_RESOLUTION):
        context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# Bot-voice formatting (plan §6).
# ---------------------------------------------------------------------------


def _placeholder(field: str) -> str:
    """Inline placeholder for an unset field — invites an Edit click."""
    return f"(tap Edit {field})"


def _direction_human(value: str | None) -> str:
    """Render direction in title case for the inline summary line."""
    if value == "long":
        return "long"
    if value == "short":
        return "short"
    return _placeholder("direction")


def _horizon_human(value: str | None) -> str:
    """Render horizon in calendar / natural language."""
    mapping = {
        "1h": "1 hour",
        "4h": "4 hours",
        "24h": "24 hours",
        "3d": "3 days",
        "7d": "1 week",
        "14d": "2 weeks",
        "30d": "1 month",
        "90d": "3 months",
    }
    return mapping.get(value or "", _placeholder("horizon"))


def _conviction_human(value: str | None) -> str:
    """Render conviction as a single user-facing word.

    No "simulated size" leak per the bot-voice rules (Q-028). The user
    gets strong > medium > weak — that's the whole picture they need.
    """
    mapping = {
        "weak": "weak",
        "medium": "medium",
        "strong": "strong",
        "very_strong": "very strong",
    }
    return mapping.get(value or "", _placeholder("conviction"))


def _calendar_date(iso: str) -> str:
    """Format an ISO timestamp as a calendar-friendly string."""
    from datetime import datetime

    if not iso:
        return "soon"
    try:
        # Accept both "2026-06-12T09:34:00Z" and "...+00:00" shapes.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "soon"
    return dt.strftime("%b %-d") if hasattr(dt, "strftime") else iso
