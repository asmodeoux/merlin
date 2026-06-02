"""Unit tests for the continuous-horizon picker step.

Pins the contract the user reported missing:

1. The picker offers quick-pick buttons keyed off the contracts'
   ``HORIZON_QUICK_PICKS`` list (24h..3m), so adding a preset is a
   one-line change in the contracts module.
2. The prompt text advertises the free-form text-reply path
   ("Or text back ... '3 hours'").
3. ``receive_horizon_text`` parses the user's reply through the
   contracts parser, validates the bounds, and either lands the
   seconds value on the draft (success) or rerenders the picker
   with a per-failure-mode hint (parse failure vs. range failure).
4. The picker callback ``alpha:hor:<seconds>`` carries integer
   seconds and lands them on ``draft['horizon_seconds']``.

PTB plumbing is mocked the same way as ``test_alpha_ui_edit_in_place``
— Update / CallbackQuery / context are MagicMocks shaped just enough
to round-trip the step under test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_bot.handlers.alpha import step_horizon
from merlin_bot.handlers.alpha.states import USER_KEY_ASSET_RESOLUTION, USER_KEY_DRAFT
from telegram import InlineKeyboardMarkup, Message


def _fake_message(*, chat_id: int = 555, message_id: int = 1) -> MagicMock:
    """Mock that passes ``isinstance(_, telegram.Message)``.

    ``ui.render`` narrows ``edit_message_text``'s return type via
    ``isinstance(_, Message)``. Plain ``MagicMock``s fail the narrow
    → the helper falls through to the send-message branch and breaks
    the "edits-in-place" assertions.
    """
    m = MagicMock(spec=Message)
    m.chat_id = chat_id
    m.message_id = message_id
    return m


def _callback_update(*, chat_id: int = 555, data: str = "alpha:edit:horizon") -> MagicMock:
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock(return_value=_fake_message(chat_id=chat_id))
    query.edit_message_reply_markup = AsyncMock()
    query.message = _fake_message(chat_id=chat_id)
    query.data = data
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=chat_id)
    update.message = None
    return update


def _text_update(*, chat_id: int = 555, text: str = "3 hours") -> MagicMock:
    update = MagicMock()
    update.callback_query = None
    update.effective_chat = MagicMock(id=chat_id)
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update.message = msg
    return update


def _context(*, user_data: dict[str, Any] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {} if user_data is None else user_data
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=_fake_message())
    ctx.bot.edit_message_reply_markup = AsyncMock()
    ctx.bot_data = {}
    return ctx


# --- prompt_horizon ---------------------------------------------------------


async def test_prompt_horizon_renders_text_reply_hint() -> None:
    """The hint that advertises the free-form text path is the
    single most user-visible deliverable of this work — pin it.
    A user who only sees the buttons (today's behaviour before the
    fix) doesn't realise they can type "3 hours" and gets trapped
    at the quick-pick presets.
    """
    update = _callback_update(data="alpha:edit:horizon")
    ctx = _context(user_data={USER_KEY_ASSET_RESOLUTION: {"coingecko_id": "ethereum"}})
    await step_horizon.prompt_horizon(update, ctx)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    rendered = update.callback_query.edit_message_text.call_args
    text = rendered.kwargs.get("text") or rendered.args[0]
    assert "text back" in text.lower(), text
    assert "3 hours" in text, text


async def test_prompt_horizon_keyboard_uses_quick_picks_with_seconds_in_callback() -> None:
    """The picker's callback_data MUST carry integer seconds (not
    string codes) so the ``on_pick`` parser can land the value on
    ``draft['horizon_seconds']`` without re-mapping. Pins the wire
    format between the keyboard builder and the callback handler.
    """
    update = _callback_update(data="alpha:edit:horizon")
    ctx = _context()
    await step_horizon.prompt_horizon(update, ctx)
    kwargs = update.callback_query.edit_message_text.call_args.kwargs
    reply_markup = kwargs["reply_markup"]
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    labels: list[str] = []
    callbacks: list[str] = []
    for row in reply_markup.inline_keyboard:
        for btn in row:
            labels.append(btn.text)
            callbacks.append(btn.callback_data or "")
    # Bot voice: human labels, never raw codes.
    assert "24 hours" in labels
    assert "3 days" in labels
    assert "1 week" in labels
    assert "3 months" in labels
    # Every callback carries integer seconds (one of the quick-picks).
    for cb in callbacks:
        assert cb.startswith("alpha:hor:")
        assert cb.removeprefix("alpha:hor:").isdigit()
    # And every quick-pick value is in the callbacks.
    from merlin_core.contracts.horizons import HORIZON_QUICK_PICKS

    for seconds in HORIZON_QUICK_PICKS:
        assert f"alpha:hor:{seconds}" in callbacks


# --- on_pick ----------------------------------------------------------------


async def test_on_pick_lands_seconds_on_draft() -> None:
    """``alpha:hor:1209600`` (2 weeks) lands as ``horizon_seconds: 1209600``
    on the draft and bounces back to the preview."""
    update = _callback_update(data="alpha:hor:1209600")
    ctx = _context(user_data={USER_KEY_DRAFT: {}})
    await step_horizon.on_pick(update, ctx)
    assert ctx.user_data[USER_KEY_DRAFT]["horizon_seconds"] == 1_209_600
    # The preview re-render edits the picker message in place.
    update.callback_query.edit_message_text.assert_awaited()


async def test_on_pick_drops_legacy_horizon_string_key() -> None:
    """If a stale draft still has the old string ``horizon`` key
    (e.g. from a hot-reloaded dev session), ``on_pick`` must drop it
    so the preview renderer reads ``horizon_seconds`` cleanly without
    colliding with leftover state.
    """
    update = _callback_update(data="alpha:hor:86400")
    ctx = _context(user_data={USER_KEY_DRAFT: {"horizon": "14d"}})
    await step_horizon.on_pick(update, ctx)
    draft = ctx.user_data[USER_KEY_DRAFT]
    assert "horizon" not in draft
    assert draft["horizon_seconds"] == 86_400


# --- receive_horizon_text ---------------------------------------------------


@pytest.mark.parametrize(
    "user_text,expected_seconds",
    [
        # Canonical forms the LLM and well-typed users emit.
        ("3 hours", 10_800),
        ("3h", 10_800),
        ("1.5 days", 129_600),
        ("2 weeks", 1_209_600),
        ("30 days", 2_592_000),
        # Lenient typing — pin the user-reported phrasings end-to-end
        # so a regression in the parser surfaces at the bot layer too,
        # not just at the contracts layer where the parser unit tests
        # live.
        ("4 days.", 4 * 86_400),
        (" 5 day ", 5 * 86_400),
        ("week", 604_800),
        ("two days", 2 * 86_400),
        ("a week", 604_800),
        ("an hour", 3_600),
        ("Two Days.", 2 * 86_400),
        ("3 HOURS", 10_800),
    ],
)
async def test_receive_horizon_text_lands_parsed_seconds(
    user_text: str,
    expected_seconds: int,
) -> None:
    """The user's free-form reply parses through the contracts helper
    and lands as integer seconds — including the sub-day case
    ("3 hours") and the lenient typing forms ("4 days.", "two days",
    "a week", bare "week") that the user explicitly called out as
    natural chat phrasing."""
    update = _text_update(text=user_text)
    ctx = _context(user_data={USER_KEY_DRAFT: {}})
    await step_horizon.receive_horizon_text(update, ctx)
    assert ctx.user_data[USER_KEY_DRAFT]["horizon_seconds"] == expected_seconds


async def test_receive_horizon_text_unparseable_rerenders_picker_with_format_hint() -> None:
    """An unparseable reply ("soon" / "tomorrow-ish" / typo) MUST
    keep the user on the picker — they fix the typo without losing
    the keyboard or the conversation state."""
    update = _text_update(text="soon-ish")
    ctx = _context(user_data={USER_KEY_DRAFT: {}})
    await step_horizon.receive_horizon_text(update, ctx)
    # Draft did NOT land a horizon.
    assert ctx.user_data[USER_KEY_DRAFT].get("horizon_seconds") is None
    # Bot replied with the format hint (re-rendered picker).
    ctx.bot.send_message.assert_awaited()
    sent = ctx.bot.send_message.call_args.kwargs
    text = sent.get("text") or ""
    assert "didn't catch that" in text.lower() or "try" in text.lower()


async def test_receive_horizon_text_out_of_range_rerenders_picker_with_range_hint() -> None:
    """A parseable but out-of-bounds reply ("30 minutes", "1 year")
    must surface the range-specific hint so the user understands
    *why* it was rejected — not just "didn't get that"."""
    update = _text_update(text="30 minutes")
    ctx = _context(user_data={USER_KEY_DRAFT: {}})
    await step_horizon.receive_horizon_text(update, ctx)
    assert ctx.user_data[USER_KEY_DRAFT].get("horizon_seconds") is None
    ctx.bot.send_message.assert_awaited()
    sent = ctx.bot.send_message.call_args.kwargs
    text = sent.get("text") or ""
    # Range hint mentions the bounds in user-facing language.
    assert "1 hour" in text and ("90 days" in text or "90 day" in text), text


async def test_receive_horizon_text_drops_legacy_horizon_key() -> None:
    """Mirror of ``on_pick``'s defence: a text reply must clean up
    the legacy string key on the draft so the preview renderer
    doesn't accidentally pick it up."""
    update = _text_update(text="2 weeks")
    ctx = _context(user_data={USER_KEY_DRAFT: {"horizon": "14d"}})
    await step_horizon.receive_horizon_text(update, ctx)
    draft = ctx.user_data[USER_KEY_DRAFT]
    assert "horizon" not in draft
    assert draft["horizon_seconds"] == 1_209_600
