"""Per-user cooldown on the ``/alpha`` paths that fire OpenRouter.

Goal: a single spammy user can't drain the OpenRouter monthly budget
(or the CoinGecko free-tier quota that the LLM extractor's downstream
search consumes). Threat model is "legitimate users hitting buttons
too fast", not adversarial abuse.

Scope — only updates that would land on ``client.draft_claim(...)``:

* ``/alpha`` command (with or without trailing args, with or without
  the bot's @-mention) — covered by ``CommandHandler("alpha", ...)``
  inside the ``/alpha`` ConversationHandler.
* Plain text from an idle user — covered by the
  ``MessageHandler(filters.TEXT & ~filters.COMMAND, ...)`` entry-point
  on the same ConversationHandler (the freeform catch-all).

Everything else (``/board``, ``/me``, callback buttons, edit-step text
replies inside an active draft, etc.) is intentionally NOT throttled
— those paths don't fire upstream paid APIs, and we don't want a
spam-fight middleware tripping the legitimate edit flow.

Per-update logic, run as a ``TypeHandler`` at ``group=-1`` so PTB
evaluates it before the conversation/command handlers in group 0:

1. If the update isn't an LLM-trigger candidate (per the
   ``_is_alpha_trigger`` heuristic) → pass through.
2. ``SET <prefix>:cd:<user_id> 1 NX EX 10`` — per-call cooldown. If
   the SET returns 0 (key existed) → reply with a short polite line
   and ``raise ApplicationHandlerStop`` so no other handler runs.
3. ``INCR <prefix>:hr:<user_id>`` + ``EXPIRE 3600`` on first hit —
   hourly burst cap. Above the cap → polite reply +
   ``ApplicationHandlerStop``.

Fail-open: if the Redis client is missing (e.g. unit tests that never
attach one) or the Redis call raises, the middleware logs a warning
and lets the update through. The user experience never regresses
because of a broken limiter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

from merlin_bot.api_dependency import BOT_DATA_REDIS_KEY
from merlin_bot.handlers.alpha.states import USER_KEY_DRAFT

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log = logging.getLogger("merlin.bot.middleware.rate_limit")

# ---------------------------------------------------------------------------
# Tunables — code constants, not env vars. These don't differ between
# environments; if they need to, change them in the same PR that ships
# the deploy.
# ---------------------------------------------------------------------------

ALPHA_COOLDOWN_SECONDS = 10
"""Minimum interval between LLM-triggering ``/alpha`` calls per user."""

ALPHA_BURST_PER_HOUR = 30
"""Hard hourly ceiling on LLM-triggering ``/alpha`` calls per user.

A user submitting a thoughtful call every 2 minutes for an entire hour
hits 30; the cap is meant to catch a runaway script, not legitimate
heavy use.
"""

REDIS_PREFIX = "merlin_bot:rl"
"""Sibling namespace to ``merlin_bot:ptb`` used by ``RedisPersistence``.

Deliberately disjoint so a future ``FLUSHDB`` on persistence (e.g.
clearing stuck conversation state) doesn't also clear active
cooldowns and vice versa.
"""


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------


def _is_alpha_command(text: str) -> bool:
    """True when ``text`` is the ``/alpha`` command (any args, any mention)."""
    if not text.startswith("/alpha"):
        return False
    # Exactly ``/alpha`` or ``/alpha <args>`` or ``/alpha@<bot>`` or
    # ``/alpha@<bot> <args>``. Anything else starting with /alpha (e.g.
    # ``/alphas``) is not the command we care about.
    rest = text[len("/alpha") :]
    return rest == "" or rest[0] in (" ", "@")


def _is_alpha_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True iff this update is about to fire ``client.draft_claim(...)``.

    Two cases the alpha ConversationHandler treats as LLM-trigger entries
    (see ``apps/bot/merlin_bot/handlers/alpha/conversation.py`` and
    ``step_freeform.py``):

    1. ``/alpha`` command — always a candidate; if it carries args the
       command handler fires the LLM immediately, if not the user is
       parked in ``AWAIT_TEXT`` and the next message they send fires it.
       Either way, throttling the command itself is the right gate.
    2. Plain text with no active draft — the freeform catch-all
       entry-point. ``USER_KEY_DRAFT`` is set the moment the LLM returns
       a draft, so its absence means the user is either idle (about to
       trigger the catch-all) or parked in ``AWAIT_TEXT`` (the very
       next text fires the LLM). Both fire ``draft_claim``.

    Plain text WITH an active draft means the user is doing an in-flow
    edit (``EDIT_THESIS`` text reply, ``EDIT_HORIZON`` "3 days", etc.).
    Those don't hit upstream APIs — leave them alone.
    """
    if update.message is None or update.effective_user is None:
        return False
    text = (update.message.text or "").strip()
    if not text:
        return False
    if _is_alpha_command(text):
        return True
    if text.startswith("/"):
        # Some other command (/board, /me, /history, ...). Never throttle.
        return False
    user_data = context.user_data or {}
    return USER_KEY_DRAFT not in user_data


# ---------------------------------------------------------------------------
# Replies — kept friendly; bot voice rules (plan §6) avoid MarkdownV2 so
# we don't have to escape user-visible punctuation.
# ---------------------------------------------------------------------------


_COOLDOWN_REPLY = (
    "Easy — give me a few seconds before the next call. " f"Try again in {ALPHA_COOLDOWN_SECONDS}s."
)
_HOURLY_REPLY = (
    "You've hit the hourly cap on new calls. Take a breath; the limit "
    "resets within the hour. Existing drafts and /board still work."
)


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def alpha_cooldown(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """``TypeHandler`` callback — throttle ``/alpha`` LLM-trigger updates."""
    if not _is_alpha_trigger(update, context):
        return
    assert update.effective_user is not None  # narrowed by _is_alpha_trigger
    assert update.message is not None

    redis: Redis | None = context.bot_data.get(BOT_DATA_REDIS_KEY)
    if redis is None:
        # Tests / dev without a wired Redis: fail open. We log so a
        # missing client in prod surfaces in Sentry instead of silently
        # disabling the limiter forever.
        _log.warning(
            "alpha_cooldown: no Redis client on bot_data[%r]; passing through",
            BOT_DATA_REDIS_KEY,
        )
        return

    user_id = update.effective_user.id
    cooldown_key = f"{REDIS_PREFIX}:alpha:cd:{user_id}"
    hourly_key = f"{REDIS_PREFIX}:alpha:hr:{user_id}"

    try:
        acquired = await redis.set(
            cooldown_key,
            "1",
            nx=True,
            ex=ALPHA_COOLDOWN_SECONDS,
        )
    except Exception as e:
        _log.warning("alpha_cooldown: redis SET failed (%s); passing through", e)
        return

    if not acquired:
        await update.message.reply_text(_COOLDOWN_REPLY)
        raise ApplicationHandlerStop

    try:
        count = await redis.incr(hourly_key)
        if count == 1:
            await redis.expire(hourly_key, 3600)
    except Exception as e:
        # Cooldown was already taken; if the hourly tracking is broken
        # we still let the call through. Worst case: a flaky Redis
        # disables the hourly cap; the per-call cooldown still bounds
        # the rate to 6/min.
        _log.warning("alpha_cooldown: redis INCR/EXPIRE failed (%s); passing through", e)
        return

    if count > ALPHA_BURST_PER_HOUR:
        await update.message.reply_text(_HOURLY_REPLY)
        raise ApplicationHandlerStop


def build_handler() -> TypeHandler[Update, ContextTypes.DEFAULT_TYPE, None]:
    """Return the ``TypeHandler`` ready to register at ``group=-1``."""
    return TypeHandler(Update, alpha_cooldown)
