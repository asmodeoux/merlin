"""Preview step — central hub of the ``/alpha`` flow.

Renders the prefilled fields as a single message + Confirm / Edit /
Cancel inline keyboard. Two voice rules still apply (plan §6): no
internal nouns (no "v0", no "$5 simulated", no "scored" / "unscored"),
and no target-price echoes beyond what the user's own thesis already
says.

We **do** use ``Field: value`` lines on the preview. The original
"summary-sentence" format (``ETH · long · 2 weeks · strong``) was
dense and hard to scan once a field was missing — placeholders
collided visually with real values. One-field-per-line keeps the
"what is what" obvious and lines up with the inline-edit buttons.

The "silent capture" rule still holds: anything in ``extracted_fields``
that the v0 verifier can't score is **not surfaced**. It lives in the
DB for the record; the user's verbatim thesis is the attestation.

The Confirm button is **hidden** until every required field is set —
``(asset, direction, horizon, conviction, thesis)``. There is no
"confirm-then-error" path. If the user is missing something, the
preview tells them which lines are blank and only the Edit / Cancel
buttons render.
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
from merlin_bot.handlers.alpha.ui import render as ui_render

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
    """Render the preview message + inline keyboard. Always returns PREVIEW.

    Edits the click-originating message in place if invoked via a
    callback (Edit-X → pick → back-to-preview), otherwise sends a new
    message (freeform entry, text-reply edits like the thesis step).
    Either way the user sees exactly one live preview card at a time.
    """
    if context.user_data is None:
        return ConversationHandler.END

    draft = context.user_data.get(USER_KEY_DRAFT, {})
    asset = context.user_data.get(USER_KEY_ASSET_RESOLUTION, {})

    text = _render_preview(draft, asset)
    keyboard = _build_preview_keyboard(draft, asset)
    await ui_render(update, context, text=text, reply_markup=keyboard)
    return AlphaState.PREVIEW


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked Confirm. POST /claims and morph the preview into a receipt.

    Editing-in-place means the user sees the very same card visibly
    change from "Tap Confirm…" → "Got it. Resolves Jun 12.". The
    keyboard disappears so a stale Confirm click can't fire twice.
    """
    query = update.callback_query
    if query is None or context.user_data is None:
        return ConversationHandler.END
    await query.answer()

    draft = context.user_data.get(USER_KEY_DRAFT, {})
    asset = context.user_data.get(USER_KEY_ASSET_RESOLUTION, {})

    # Defence in depth — the Confirm button only renders when
    # ``_is_draft_complete`` is True, so this should be unreachable in
    # normal traffic. A stale message clicked after state changed is
    # the realistic edge case.
    tg_user_id = update.effective_user.id if update.effective_user is not None else None
    payload = _build_submit_payload(draft, asset, tg_user_id=tg_user_id)
    if payload is None:
        await ui_render(
            update,
            context,
            text="Looks like a field is still missing. Hit Edit on what's blank.",
            reply_markup=_build_preview_keyboard(draft, asset),
        )
        return AlphaState.PREVIEW

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.submit_claim(payload=payload)
    except MerlinHTTPError as e:
        _log.warning("submit_claim failed: %s", e)
        await ui_render(
            update,
            context,
            text="Something's off on our end. Try /alpha again in a moment.",
            reply_markup=None,
        )
        return ConversationHandler.END

    receipt = _render_receipt(result=result, draft=draft, asset=asset)
    await ui_render(update, context, text=receipt, reply_markup=None)

    _clear(context)
    return ConversationHandler.END


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked Cancel. Morph the preview into the goodbye line.

    Same in-place edit — the buttons disappear and the message becomes
    the one-liner. The user gets immediate visible feedback that the
    click registered (the original UX bug: just a loading spinner with
    no follow-up).
    """
    query = update.callback_query
    if query is not None:
        await query.answer()
    await ui_render(
        update,
        context,
        text="No problem. Hit /alpha when you've got something to call.",
        reply_markup=None,
    )
    _clear(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_preview(draft: dict[str, Any], asset: dict[str, Any]) -> str:
    """Compose the preview text. Bot-voice rules strictly enforced.

    One field per line — ``Field: value`` — with placeholders pointing
    at the corresponding Edit / Set button when a field is missing.
    The Confirm affordance is only mentioned in the closing line when
    the draft is complete; otherwise the closing line tells the user
    what's blank.
    """
    asset_symbol = asset.get("symbol") or _missing_placeholder("Asset")
    direction = _direction_human(draft.get("direction"))
    horizon = _horizon_human(draft.get("horizon_seconds"))
    conviction = _conviction_human(draft.get("conviction"))
    thesis = (draft.get("thesis_text") or "").strip() or _missing_placeholder("Thesis")

    is_complete = _is_draft_complete(draft, asset)
    footer = (
        "Tap ✅ Confirm if it looks right, or ✏️ Edit a field."
        if is_complete
        else "Fill in the missing fields above to confirm."
    )

    lines = [
        "Here's your call:",
        "",
        f"Asset: {asset_symbol}",
        f"Direction: {direction}",
        f"Horizon: {horizon}",
        f"Conviction: {conviction}",
        "",
        f'"{thesis}"',
        "",
        footer,
    ]
    return "\n".join(lines)


def _render_receipt(
    *,
    result: dict[str, Any],
    draft: dict[str, Any],
    asset: dict[str, Any],
) -> str:
    """Receipt message shown after a successful submit.

    Mirrors the preview layout so the user can verify field-by-field
    that what landed on the server matches what they confirmed.
    Adds two timestamp lines (submitted + resolves) in **full UTC**
    so the user knows exactly when the bot will check the call —
    "in 2 weeks" was ambiguous (does it count weekends? what
    timezone?). Timestamps come straight from the server response so
    there's no client-clock skew.

    The entry-price line ("Entry price: $X.YZ") only renders when the
    server captured a snapshot at submit. If CoinGecko was down at
    submit time the field is ``None`` and we omit the line — never
    invent a placeholder, never tell the user "$0.00".
    """
    # Prefer the canonical fields the server echoed back; fall back to
    # the local draft if a field is missing (defensive — should be
    # exhaustively populated by ``ClaimResponse``).
    asset_symbol = result.get("asset_symbol") or asset.get("symbol") or "your call"
    direction = _direction_human(result.get("direction") or draft.get("direction"))
    horizon = _horizon_human(result.get("horizon_seconds") or draft.get("horizon_seconds"))
    conviction = _conviction_human(result.get("conviction") or draft.get("conviction"))
    thesis = (result.get("thesis_text") or draft.get("thesis_text") or "").strip()

    submitted_at = _utc_datetime(result.get("submitted_at", ""))
    resolves_at = _utc_datetime(result.get("resolves_at", ""))
    entry_price_line = _entry_price_line(
        result.get("price_usd_at_submit"),
        asset_symbol,
    )

    lines: list[str | None] = [
        "✅ Saved.",
        "",
        f"Asset: {asset_symbol}",
        f"Direction: {direction}",
        f"Horizon: {horizon}",
        f"Conviction: {conviction}",
        # Entry-price line — None when the server didn't capture a
        # snapshot (CoinGecko down at submit, legacy rows). Filtered
        # out by the ``is not None`` join below.
        entry_price_line,
        "",
        f'"{thesis}"' if thesis else "",
        "" if thesis else None,
        f"Submitted: {submitted_at}",
        f"Resolves:  {resolves_at}",
        "",
        "See /me for your open calls.",
    ]
    return "\n".join(line for line in lines if line is not None)


def _build_preview_keyboard(
    draft: dict[str, Any],
    asset: dict[str, Any],
) -> InlineKeyboardMarkup:
    """Build the Edit / (optional) Confirm / Cancel keyboard.

    Per-field label discriminates **Set** vs **Edit**:

    * Field has a value already → ``"✏️ Edit Asset"`` (tweak what's
      there).
    * Field is empty → ``"📝 Set Asset"`` (this is an explicit
      missing-field action, not an optional touch-up).

    Same callback_data either way — the action is identical, only
    the label changes. Users reported the old uniform "✏️ Edit X"
    label hid the difference between "Foo is filled, you can change
    it" and "Foo is blank, you have to set it before Confirm
    renders".

    Confirm is **omitted** until every required field has a value.
    The user can never click Confirm on an incomplete bet — there's
    no "confirm-then-error" UX. (Defence in depth: ``on_confirm``
    still re-validates via ``_build_submit_payload`` in case a stale
    message is clicked on after state changed.)
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _field_button_label("Asset", asset.get("coingecko_id")),
                callback_data=CB_EDIT_ASSET,
            ),
            InlineKeyboardButton(
                _field_button_label("Direction", draft.get("direction")),
                callback_data=CB_EDIT_DIRECTION,
            ),
        ],
        [
            InlineKeyboardButton(
                _field_button_label("Horizon", draft.get("horizon_seconds")),
                callback_data=CB_EDIT_HORIZON,
            ),
            InlineKeyboardButton(
                _field_button_label("Conviction", draft.get("conviction")),
                callback_data=CB_EDIT_CONVICTION,
            ),
        ],
        [
            InlineKeyboardButton(
                _field_button_label("Thesis", (draft.get("thesis_text") or "").strip()),
                callback_data=CB_EDIT_THESIS,
            ),
        ],
    ]
    if _is_draft_complete(draft, asset):
        rows.insert(0, [InlineKeyboardButton("✅ Confirm", callback_data=CB_CONFIRM)])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _is_draft_complete(draft: dict[str, Any], asset: dict[str, Any]) -> bool:
    """True iff every required field for a valid claim is set.

    Mirrors ``_build_submit_payload``'s preconditions so the keyboard and
    the validation guard agree byte-for-byte — if this flips True, the
    submit will succeed; if False, Confirm doesn't render in the first
    place.
    """
    if not asset or not asset.get("coingecko_id"):
        return False
    if not draft.get("direction"):
        return False
    if not draft.get("horizon_seconds"):
        return False
    if not draft.get("conviction"):
        return False
    return bool((draft.get("thesis_text") or "").strip())


def _build_submit_payload(
    draft: dict[str, Any],
    asset: dict[str, Any],
    *,
    tg_user_id: int | None,
) -> dict[str, Any] | None:
    """Compose the ``SubmitClaimRequest`` payload; None if a required field is missing.

    ``tg_user_id`` is required by the API to look up the calling
    source via ``source_identity`` (the HMAC bearer authenticates the
    service, not the per-user identity). A missing ``tg_user_id``
    means we're being driven by a ``Update`` with no effective user
    (e.g. a channel post replay) — drop the payload so the route
    never sees a half-populated body.
    """
    if not asset or not asset.get("coingecko_id"):
        return None
    if tg_user_id is None:
        return None
    direction = draft.get("direction")
    horizon_seconds = draft.get("horizon_seconds")
    conviction = draft.get("conviction")
    thesis_text = (draft.get("thesis_text") or "").strip()
    if not (direction and horizon_seconds and conviction and thesis_text):
        return None
    return {
        "coingecko_id": asset["coingecko_id"],
        "direction": direction,
        "horizon_seconds": horizon_seconds,
        "conviction": conviction,
        "thesis_text": thesis_text,
        "tg_user_id": tg_user_id,
        "asset_query": draft.get("asset_query"),
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


def _field_button_label(field_title: str, current_value: object) -> str:
    """Return ``"📝 Set X"`` for an unset field, ``"✏️ Edit X"`` otherwise.

    The single source of truth for the per-field button label —
    used by ``_build_preview_keyboard`` and mirrored by
    ``_missing_placeholder`` so the inline placeholder text and the
    button label stay in lockstep (user's eye jumps from blank line
    to matching button without translation).
    """
    if current_value:
        return f"✏️ Edit {field_title}"
    return f"📝 Set {field_title}"


def _missing_placeholder(field_title: str) -> str:
    """Inline placeholder shown next to a field with no value yet.

    Mirrors ``_field_button_label``'s "Set" branch — the button that
    fixes the blank uses the same emoji + verb, so the eye doesn't
    have to translate "(tap Edit X)" → "📝 Set X".
    """
    return f"(tap 📝 Set {field_title})"


def _direction_human(value: str | None) -> str:
    """Render direction in title case for the preview line."""
    if value == "long":
        return "Long"
    if value == "short":
        return "Short"
    return _missing_placeholder("Direction")


def _horizon_human(value: object) -> str:
    """Render horizon (integer seconds) in natural language.

    The wire format is integer seconds in
    ``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]``; the contracts
    ``humanise_horizon`` helper picks the cleanest unit (1 hour /
    3 hours / 24 hours / 1 week / 30 days / 2 months / 3 months).
    Falls back to the missing-field placeholder when the value is
    blank, ``None``, or any non-numeric junk (defensive — should
    only happen on a stale draft).
    """
    from merlin_core.contracts.horizons import humanise_horizon

    if value is None or value in {"", 0}:
        return _missing_placeholder("Horizon")
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return _missing_placeholder("Horizon")
    if seconds <= 0:
        return _missing_placeholder("Horizon")
    return humanise_horizon(seconds)


def _conviction_human(value: str | None) -> str:
    """Render conviction as a single user-facing word.

    No "simulated size" leak per the bot-voice rules (Q-028). The user
    gets Strong > Medium > Weak — that's the whole picture they need.
    """
    mapping = {
        "weak": "Weak",
        "medium": "Medium",
        "strong": "Strong",
        "very_strong": "Very strong",
    }
    return mapping.get(value or "", _missing_placeholder("Conviction"))


def _entry_price_line(price: object, asset_symbol: str) -> str | None:
    """Render the "Entry price: $X.YZ ASSET" line, or ``None`` to omit.

    ``price`` comes off the ``ClaimResponse.price_usd_at_submit``
    field — server may return ``None`` when CoinGecko was down at
    submit time. In that case we omit the line entirely (the bot
    never invents a placeholder, never tells the user "$0.00").

    Formatting: dollar-sign + two-decimal USD for the round-number
    bucket, four decimals for sub-dollar tickers (memecoins, AI
    micro-caps). The asset symbol is appended so a user scanning a
    list of receipts can tell at a glance which currency this
    relates to.
    """
    if price is None:
        return None
    try:
        amount = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f"Entry price: {_format_usd(amount)} per {asset_symbol}"


def _format_usd(amount: float) -> str:
    """Render a USD amount with adaptive decimals.

    Big tickers (BTC, ETH) read better at 2dp; sub-dollar tickers
    need more digits to be meaningful ($0.0003 vs an unhelpful
    $0.00). Threshold at $1: above is 2dp with thousands separator;
    below is 4dp; tiny values (< $0.01) get 8dp so a $0.00000123
    memecoin entry doesn't render as $0.00.
    """
    if amount >= 1.0:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.4f}"
    return f"${amount:.8f}"


def _calendar_date(iso: str) -> str:
    """Format an ISO timestamp as a calendar-friendly short string.

    Kept for tests and any caller that wants a compact "Jun 12" style.
    The receipt itself uses ``_utc_datetime`` for an unambiguous full
    timestamp.
    """
    from datetime import datetime

    if not iso:
        return "soon"
    try:
        # Accept both "2026-06-12T09:34:00Z" and "...+00:00" shapes.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "soon"
    return dt.strftime("%b %-d") if hasattr(dt, "strftime") else iso


def _utc_datetime(iso: str) -> str:
    """Format an ISO timestamp as an unambiguous UTC date+time string.

    Output shape: ``2026-06-12 16:32 UTC`` — minute-precision, no
    seconds (the resolver's window is ±5 minutes anyway, so seconds
    convey false precision). Always normalised to UTC; if the
    timestamp comes in with an offset, we convert.

    Falls back to ``"soon"`` on a missing / unparseable value so the
    receipt never crashes the bot on a malformed server response.
    """
    from datetime import UTC, datetime

    if not iso:
        return "soon"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "soon"
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
