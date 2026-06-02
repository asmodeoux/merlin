"""UX regression suite — every callback path must edit-in-place.

The Sprint-1 live bug: clicking Cancel showed the loading spinner on
the button and then... nothing. The bot sent a new message instead of
editing the preview, so the buttons stayed clickable forever and the
user got no confirmation.

These tests pin the contract that **every callback-driven step in the
``/alpha`` flow edits the originating message** rather than spawning a
new one. They also pin that text-reply paths still send fresh messages
(there's nothing to edit when the user just typed).

We mock ``telegram.Update`` / ``telegram.CallbackQuery`` / the bot
directly — the PTB classes are heavy and not friendly to unit-level
poking. The mocks model only the surface the steps actually call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from merlin_bot.handlers.alpha import (
    step_conviction,
    step_direction,
    step_horizon,
    step_preview,
    step_thesis,
)
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_DRAFT,
)
from merlin_bot.handlers.alpha.ui import render as ui_render
from telegram import Message
from telegram.error import BadRequest

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _fake_message(*, chat_id: int = 555, message_id: int = 1) -> MagicMock:
    """A mock that passes ``isinstance(_, telegram.Message)``.

    ``ui.render`` narrows ``edit_message_text``'s return type via
    ``isinstance(_, telegram.Message)``. Plain ``MagicMock``s fail
    that narrow → the helper falls through to the send-message branch
    and breaks the "edits-in-place" assertions. ``spec=Message`` makes
    the mock satisfy isinstance without bringing in a real Message.
    """
    m = MagicMock(spec=Message)
    m.chat_id = chat_id
    m.message_id = message_id
    return m


def _callback_update(*, chat_id: int = 555, data: str = "alpha:cancel") -> MagicMock:
    """Build an Update that looks like a button click."""
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


def _text_update(*, chat_id: int = 555, text: str = "ETH") -> MagicMock:
    """Build an Update that looks like a plain text reply."""
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


_FULL_DRAFT = {
    "direction": "long",
    "horizon_seconds": 1_209_600,
    "conviction": "strong",
    "thesis_text": "Vitalik rumour",
}
_FULL_ASSET = {"coingecko_id": "ethereum", "symbol": "ETH"}


# ---------------------------------------------------------------------------
# ui.render contract
# ---------------------------------------------------------------------------


async def test_ui_render_edits_in_place_on_callback() -> None:
    """Callback updates → edit_message_text; never send_message."""
    update = _callback_update()
    ctx = _context()
    await ui_render(update, ctx, text="hi", reply_markup=None)
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()


async def test_ui_render_sends_new_message_on_text_reply() -> None:
    """Text reply updates → send_message; never edit_message_text."""
    update = _text_update()
    ctx = _context()
    await ui_render(update, ctx, text="hi", reply_markup=None)
    ctx.bot.send_message.assert_awaited_once()


async def test_ui_render_swallows_message_not_modified() -> None:
    """Editing to identical content is a no-op, never an error.

    Without this guard, repeatedly clicking the same Edit button (or
    racing two clicks) would raise BadRequest to the user."""
    update = _callback_update()
    update.callback_query.edit_message_text.side_effect = BadRequest("Message is not modified")
    ctx = _context()
    await ui_render(update, ctx, text="hi", reply_markup=None)
    # We MUST NOT fall back to send_message when content is identical
    # — that would post a duplicate message.
    ctx.bot.send_message.assert_not_awaited()


async def test_ui_render_falls_back_to_send_on_other_badrequest() -> None:
    """An unedittable message (e.g. >48h old) MUST NOT silently drop
    the user — fall back to sending fresh content so the user sees a
    response to their click."""
    update = _callback_update()
    update.callback_query.edit_message_text.side_effect = BadRequest("message can't be edited")
    ctx = _context()
    await ui_render(update, ctx, text="hi", reply_markup=None)
    ctx.bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Step coroutines — Cancel / Confirm / Edit-X / Thesis prompt
# ---------------------------------------------------------------------------


async def test_on_cancel_edits_preview_in_place_and_strips_keyboard() -> None:
    """Cancel must visibly morph the preview card into the goodbye
    line — no new message, no leftover buttons."""
    update = _callback_update(data="alpha:cancel")
    ctx = _context(user_data={USER_KEY_DRAFT: _FULL_DRAFT, USER_KEY_ASSET_RESOLUTION: _FULL_ASSET})
    await step_preview.on_cancel(update, ctx)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()
    # Stripping the keyboard = reply_markup=None on the edit.
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert kwargs.get("reply_markup") is None
    # User data dropped — Cancel doesn't preserve the draft for next time.
    assert USER_KEY_DRAFT not in ctx.user_data
    assert USER_KEY_ASSET_RESOLUTION not in ctx.user_data


async def test_on_confirm_edits_preview_into_receipt() -> None:
    """Confirm-success must morph the preview into the receipt; no
    new message, no leftover buttons (otherwise a stale Confirm could
    fire a duplicate submit)."""
    update = _callback_update(data="alpha:confirm")
    ctx = _context(
        user_data={
            USER_KEY_DRAFT: dict(_FULL_DRAFT),
            USER_KEY_ASSET_RESOLUTION: dict(_FULL_ASSET),
        }
    )
    fake_client = MagicMock()
    fake_client.submit_claim = AsyncMock(
        return_value={
            "asset_symbol": "ETH",
            "resolves_at": "2026-06-12T09:34:00Z",
        }
    )
    from merlin_bot.api_dependency import BOT_DATA_KEY

    ctx.bot_data[BOT_DATA_KEY] = fake_client
    await step_preview.on_confirm(update, ctx)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert "Resolves" in kwargs.get("text", "")
    assert kwargs.get("reply_markup") is None


async def test_prompt_direction_edits_preview_into_picker() -> None:
    """Edit-X clicks must morph the preview card into the picker,
    not spawn a second message. (Sprint-1 bug: clicking Edit Direction
    left the preview keyboard behind and added another card below.)"""
    update = _callback_update(data="alpha:edit:direction")
    ctx = _context()
    await step_direction.prompt_direction(update, ctx)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert "Which way" in kwargs.get("text", "")
    # Picker must have a keyboard — long/short are the only ways forward.
    assert kwargs.get("reply_markup") is not None


async def test_prompt_horizon_edits_preview_into_picker() -> None:
    update = _callback_update(data="alpha:edit:horizon")
    ctx = _context()
    await step_horizon.prompt_horizon(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()


async def test_prompt_conviction_edits_preview_into_picker() -> None:
    update = _callback_update(data="alpha:edit:conviction")
    ctx = _context()
    await step_conviction.prompt_conviction(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()


async def test_prompt_thesis_edits_preview_into_text_prompt_with_no_keyboard() -> None:
    """Edit Thesis can't keep a keyboard — the next input is text.
    But we still edit-in-place so the preview keyboard disappears."""
    update = _callback_update(data="alpha:edit:thesis")
    ctx = _context()
    await step_thesis.prompt_thesis(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert kwargs.get("reply_markup") is None


# ---------------------------------------------------------------------------
# show_preview's dual mode
# ---------------------------------------------------------------------------


async def test_show_preview_edits_in_place_after_callback_pick() -> None:
    """After a picker click (direction/horizon/conviction/asset),
    show_preview must edit the picker message BACK into the preview —
    one evolving message, never a growing stack."""
    update = _callback_update(data="alpha:dir:long")
    ctx = _context(
        user_data={
            USER_KEY_DRAFT: dict(_FULL_DRAFT),
            USER_KEY_ASSET_RESOLUTION: dict(_FULL_ASSET),
        }
    )
    await step_preview.show_preview(update, ctx)
    update.callback_query.edit_message_text.assert_awaited_once()
    ctx.bot.send_message.assert_not_awaited()


async def test_show_preview_sends_new_message_on_freeform_entry() -> None:
    """The freeform-entry path (user typed plain text → LLM draft)
    has no message to edit. show_preview must send a fresh card."""
    update = _text_update(text="ETH long 2 weeks")
    ctx = _context(
        user_data={
            USER_KEY_DRAFT: dict(_FULL_DRAFT),
            USER_KEY_ASSET_RESOLUTION: dict(_FULL_ASSET),
        }
    )
    await step_preview.show_preview(update, ctx)
    ctx.bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Pick handlers route back through show_preview
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,fn_name,callback_data,field,value",
    [
        (step_direction, "on_pick", step_direction.CB_LONG, "direction", "long"),
        (
            step_horizon,
            "on_pick",
            f"{step_horizon.CB_PREFIX}1209600",
            "horizon_seconds",
            1_209_600,
        ),
        (
            step_conviction,
            "on_pick",
            f"{step_conviction.CB_PREFIX}strong",
            "conviction",
            "strong",
        ),
    ],
)
async def test_pick_handlers_edit_picker_back_into_preview(
    module: Any,
    fn_name: str,
    callback_data: str,
    field: str,
    value: Any,
) -> None:
    update = _callback_update(data=callback_data)
    ctx = _context(user_data={USER_KEY_ASSET_RESOLUTION: dict(_FULL_ASSET)})
    await getattr(module, fn_name)(update, ctx)
    # Field landed on the draft.
    assert ctx.user_data[USER_KEY_DRAFT][field] == value
    # show_preview ran in edit-in-place mode (same callback_query):
    # one edit, zero new messages.
    update.callback_query.edit_message_text.assert_awaited()
    ctx.bot.send_message.assert_not_awaited()
