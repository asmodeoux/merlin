"""Horizon step — quick-pick buttons + free-form text reply.

Horizons are continuous integer seconds in
``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]``. The picker surfaces
a curated set of quick-picks (the historical buckets) for fast
tapping, and also accepts a free-form text reply ("3 hours",
"1.5 days", "2 weeks") so the user is never trapped at one of the
preset values. The hint line under the buttons advertises the text
option so users notice it.
"""

from __future__ import annotations

import logging

from merlin_core.contracts.horizons import (
    HORIZON_QUICK_PICKS,
    HorizonParseError,
    HorizonRangeError,
    humanise_horizon,
    parse_horizon_text,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.handlers.alpha.states import (
    USER_KEY_DRAFT,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview
from merlin_bot.handlers.alpha.ui import render as ui_render

_log = logging.getLogger("merlin.bot.handlers.alpha.horizon")

CB_PREFIX = "alpha:hor:"

# Hint shown under the quick-pick buttons so the text-reply path is
# discoverable. Phrasing matches the bot-voice rules — no version
# markers, no internal jargon, plain natural-language examples.
_PICKER_PROMPT = (
    "How long until you find out?\n"
    'Tap a preset, or text back your own — e.g. "3 hours", '
    '"1.5 days", "2 weeks".'
)

# When the user texts back something we can't read or that's out of
# range, we re-render the picker with one of these prepended so they
# see exactly what went wrong without leaving the step.
_INVALID_FORMAT_HINT = 'I didn\'t catch that. Try "3 hours", "2 weeks", "30 days".'
_INVALID_RANGE_HINT = (
    "That's outside what I can resolve. Pick something between 1 hour and 90 days."
)


async def prompt_horizon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Morph the preview card into the horizon picker.

    Builds a quick-pick keyboard from ``HORIZON_QUICK_PICKS`` so the
    button labels stay in lockstep with the humaniser — adding a
    preset is a single-line change in ``merlin_core.contracts.horizons``.
    """
    query = update.callback_query
    if query is not None:
        await query.answer()
    await ui_render(
        update,
        context,
        text=_PICKER_PROMPT,
        reply_markup=_build_picker_keyboard(),
    )
    return AlphaState.EDIT_HORIZON


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the picked horizon (seconds); back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None or not query.data:
        return ConversationHandler.END
    await query.answer()
    raw = query.data.removeprefix(CB_PREFIX)
    try:
        seconds = int(raw)
    except ValueError:
        # Defensive — should be unreachable since we built the
        # callback_data ourselves above. Log and bounce back.
        _log.warning("horizon picker callback had non-int payload: %r", raw)
        return await show_preview(update, context)
    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["horizon_seconds"] = seconds
    # Drop the legacy string key if a stale draft still has it so the
    # preview renderer (which now reads ``horizon_seconds``) doesn't
    # accidentally collide with leftover dev / test state.
    draft.pop("horizon", None)
    return await show_preview(update, context)


async def receive_horizon_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Parse a free-form horizon reply and store it.

    Accepts forms the domain parser handles: ``"3 hours"``, ``"1.5d"``,
    ``"2 weeks"``, ``"30min"``. On parse failure or range failure we
    stay on the picker and prepend the relevant hint to the prompt —
    the user fixes the typo without losing the keyboard.
    """
    if update.message is None or context.user_data is None:
        return AlphaState.EDIT_HORIZON
    text = (update.message.text or "").strip()
    try:
        seconds = parse_horizon_text(text)
    except HorizonRangeError:
        await ui_render(
            update,
            context,
            text=f"{_INVALID_RANGE_HINT}\n\n{_PICKER_PROMPT}",
            reply_markup=_build_picker_keyboard(),
        )
        return AlphaState.EDIT_HORIZON
    except HorizonParseError:
        await ui_render(
            update,
            context,
            text=f"{_INVALID_FORMAT_HINT}\n\n{_PICKER_PROMPT}",
            reply_markup=_build_picker_keyboard(),
        )
        return AlphaState.EDIT_HORIZON

    draft = context.user_data.setdefault(USER_KEY_DRAFT, {})
    draft["horizon_seconds"] = seconds
    draft.pop("horizon", None)
    return await show_preview(update, context)


def _build_picker_keyboard() -> InlineKeyboardMarkup:
    """Build the quick-pick keyboard from ``HORIZON_QUICK_PICKS``.

    Layout: two columns. ``HORIZON_QUICK_PICKS`` is sorted shortest-
    first, so the user reads "24 hours, 3 days" on the top row and
    progresses to "1 month, 3 months" at the bottom — matches the
    natural mental ordering.
    """
    buttons = [
        InlineKeyboardButton(
            humanise_horizon(seconds),
            callback_data=f"{CB_PREFIX}{seconds}",
        )
        for seconds in HORIZON_QUICK_PICKS
    ]
    rows: list[list[InlineKeyboardButton]] = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)
