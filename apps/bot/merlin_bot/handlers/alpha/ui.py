"""Shared UI helpers for the ``/alpha`` flow — edit-in-place ergonomics.

Telegram inline keyboards give us a single message that we can mutate as
the user clicks through. Best-in-class bot UX is to **edit the same
message** as the user navigates rather than spawning new messages — the
chat stays clean, stale keyboards never linger, and the loading spinner
on a button click is immediately followed by the message visibly
changing.

Rules of thumb encoded here:

* When an update came in via ``callback_query`` (button click), we
  edit the originating message in place. ``answer()`` kills the
  spinner; ``edit_message_text`` swaps the text + keyboard.
* When an update came in via a text reply (the user typed something),
  we have to send a new message — there's no message to edit. The
  prior callback-side message should have had its keyboard stripped
  before the prompt was shown.
* Telegram returns ``BadRequest("message is not modified")`` when an
  edit's content is byte-identical to the existing message. That's a
  no-op from the user's perspective; we swallow it silently.
* Any other ``BadRequest`` (expired callback, message-too-old, etc.)
  falls back to sending a new message so the user is never left in
  silence after a click.

These helpers are intentionally NOT generic — they encode the
``/alpha`` flow's specific conventions (the bot is invoked either via
a text reply or a callback click; we never need to edit messages from
unrelated chats). Keep them here, not in some shared "kit", to make
this contract obvious to anyone reading the step files.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from merlin_bot.handlers.alpha.states import (
    USER_KEY_PREVIEW_CHAT_ID,
    USER_KEY_PREVIEW_MESSAGE_ID,
)

_log = logging.getLogger("merlin.bot.handlers.alpha.ui")

_NOT_MODIFIED_SUBSTRING = "message is not modified"


async def render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    disable_link_preview: bool = False,
) -> Message | None:
    """Edit the click-originating message in place, or send a fresh one.

    When ``update.callback_query`` is set (the user clicked a button),
    we edit the message that owns that keyboard so the chat doesn't
    accumulate stale-button cards. When the update was a text reply,
    we send a new message — there's nothing to edit.

    Set ``reply_markup=None`` to strip a keyboard while keeping the
    text (used for terminal screens like Cancel and Confirm receipts).

    ``parse_mode`` opts the message into a specific Telegram parser
    (``"MarkdownV2"``, ``"HTML"``). Defaults to ``None`` because the
    bot's global ``Defaults`` is parse-mode-free — handlers opt in
    per-message. ``disable_link_preview=True`` suppresses Telegram's
    automatic preview card for the first URL in the body (used by the
    asset picker so the chat doesn't grow a CoinGecko thumbnail under
    every disambiguation).

    Side effect: every successful render also updates the
    ``USER_KEY_PREVIEW_*`` slots in ``context.user_data`` to point at
    the live card. That way text-based exits (the ``/cancel`` command,
    a future conversation timeout) can locate and strip the dangling
    keyboard. Callers that want to render WITHOUT updating the live-
    card pointer (rare — only the terminal cancel/receipt screens
    where the card we just edited IS itself the live card) get the
    Message back and can choose to do nothing with it.

    Returns the resulting ``Message`` (the edited one or the new one)
    when known; ``None`` if PTB returned ``True`` from an edit (which
    means "edited but I'm not giving you the object").
    """
    link_preview = LinkPreviewOptions(is_disabled=True) if disable_link_preview else None
    query = update.callback_query
    result_msg: Message | None = None
    if query is not None:
        try:
            edit_kwargs: dict[str, Any] = {
                "text": text,
                "reply_markup": reply_markup,
            }
            if parse_mode is not None:
                edit_kwargs["parse_mode"] = parse_mode
            if link_preview is not None:
                edit_kwargs["link_preview_options"] = link_preview
            edited = await query.edit_message_text(**edit_kwargs)
            # ``edit_message_text`` returns ``Message | bool`` and
            # ``query.message`` is typed as ``MaybeInaccessibleMessage``
            # (PTB v21). For messages we sent ourselves — always the
            # case here — both resolve to a real ``Message``. The
            # isinstance() narrows the type and silently drops the
            # rare inaccessible case (in which the tracking is moot
            # anyway).
            candidate: object = edited if isinstance(edited, Message) else query.message
            result_msg = candidate if isinstance(candidate, Message) else None
        except BadRequest as e:
            # "message is not modified" → identical content; nothing to
            # do, the user already sees the right thing.
            if _NOT_MODIFIED_SUBSTRING in str(e).lower():
                msg = query.message
                return msg if isinstance(msg, Message) else None
            # Any other edit failure (e.g. message too old to edit) —
            # don't abandon the user; fall back to sending a new one.
            _log.warning("edit_message_text fell back to send_message: %s", e)
            result_msg = None

    if result_msg is None:
        if update.effective_chat is None:
            return None
        send_kwargs: dict[str, Any] = {
            "chat_id": update.effective_chat.id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if parse_mode is not None:
            send_kwargs["parse_mode"] = parse_mode
        if link_preview is not None:
            send_kwargs["link_preview_options"] = link_preview
        result_msg = await context.bot.send_message(**send_kwargs)

    _remember_live_card(context, result_msg, has_keyboard=reply_markup is not None)
    return result_msg


def _remember_live_card(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    *,
    has_keyboard: bool,
) -> None:
    """Track the chat+message id of the most recent card with a keyboard.

    Cards without keyboards (the goodbye line, the receipt) are terminal
    — there's nothing left to strip — so we clear the slots instead.
    """
    if context.user_data is None:
        return
    if has_keyboard:
        context.user_data[USER_KEY_PREVIEW_MESSAGE_ID] = message.message_id
        context.user_data[USER_KEY_PREVIEW_CHAT_ID] = message.chat_id
    else:
        context.user_data.pop(USER_KEY_PREVIEW_MESSAGE_ID, None)
        context.user_data.pop(USER_KEY_PREVIEW_CHAT_ID, None)


async def strip_live_card_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove the inline keyboard from the last live preview/picker card.

    Called from text-based exits (``/cancel`` typed as a command, the
    eventual timeout fallback) so the user's chat doesn't keep an
    actionable card from a flow they just abandoned.

    Silent no-op if nothing was tracked, or if the edit fails (the
    message may already be gone, or too old to edit) — failure here
    must not mask the primary exit feedback.
    """
    if context.user_data is None:
        return
    chat_id = context.user_data.pop(USER_KEY_PREVIEW_CHAT_ID, None)
    msg_id = context.user_data.pop(USER_KEY_PREVIEW_MESSAGE_ID, None)
    if chat_id is None or msg_id is None:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=None,
        )
    except BadRequest as e:
        _log.debug("could not strip stale keyboard (msg %s): %s", msg_id, e)
