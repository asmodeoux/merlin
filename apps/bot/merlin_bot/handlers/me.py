"""``/me`` handler — the user's own open calls + headline stats.

Stateless. Hits ``GET /me?tg_user_id=...`` and renders the result in
the same one-field-per-line shape as the /alpha receipt so the user
can scan their open positions without parsing dense prose.

Bot-voice rules (plan §6):

* Honest empty-state. Zero open calls reads "No open calls yet" rather
  than a blank list.
* No internal jargon. We never say ``source``, ``claim``, ``hypothesis``,
  ``regret``, ``belief score``, or any version marker — just "your call",
  "your open calls", "your hit rate".
* Accuracy is rendered as "tracking your first resolutions" when no
  call has resolved yet — never as "0.0%" or "0/0". The contract makes
  the field ``Optional`` precisely so we don't have to fake a zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.me")


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hit ``GET /me`` and reply with the rendered summary."""
    if update.message is None or update.effective_user is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        payload = await client.get_me(tg_user_id=update.effective_user.id)
    except MerlinHTTPError as e:
        if e.status_code == 404:
            # User isn't onboarded — the slash menu showed /me but
            # they never paid the /start toll. Point them at /start
            # rather than silently failing.
            await update.message.reply_text(_not_onboarded_text())
            return
        _log.warning("get_me failed: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return
    except Exception as e:
        _log.warning("get_me transport error: %s", e)
        await update.message.reply_text(_temporary_error_text())
        return

    await update.message.reply_text(_render_me(payload))


def build_handler() -> CommandHandler:
    """Wire the ``/me`` CommandHandler."""
    return CommandHandler("me", me_command)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_me(payload: dict[str, Any]) -> str:
    """Compose the ``/me`` reply.

    Layout (header + headline stats + optional list + footer call to
    action):

        <display_name> — your calls

        Open: 3   Resolved: 0
        Accuracy: tracking your first resolutions

        Open calls:
          ETH long — 2 weeks, resolves 2026-06-12 16:35 UTC
          BTC long — 1 month, resolves 2026-06-29 09:10 UTC

        /alpha to add another, /board for the leaderboard.

    Zero-open empty-state collapses the list section to a single
    "No open calls yet" line so the message stays short.
    """
    display_name = payload.get("display_name") or "you"
    open_calls = payload.get("calls_open") or []
    resolved_total = int(payload.get("calls_resolved_total") or 0)
    accuracy_line = _accuracy_line(payload.get("accuracy_pct"), resolved_total)

    header = f"{display_name} — your calls"
    headline = f"Open: {len(open_calls)}   Resolved: {resolved_total}"

    if open_calls:
        rendered_calls = "\n".join(_render_open_call(c) for c in open_calls)
        body = "Open calls:\n" + rendered_calls
    else:
        body = "No open calls yet. /alpha to make your first."

    footer = "/alpha to add another, /board for the leaderboard."

    return "\n\n".join([header, f"{headline}\n{accuracy_line}", body, footer])


def _render_open_call(call: dict[str, Any]) -> str:
    """One open-call bullet line."""
    asset = str(call.get("asset_symbol") or "?").upper()
    direction = _direction_human(str(call.get("direction") or ""))
    horizon = _horizon_human(str(call.get("horizon") or ""))
    resolves_at = _utc_datetime(str(call.get("resolves_at") or ""))
    return f"  {asset} {direction} — {horizon}, resolves {resolves_at}"


def _accuracy_line(accuracy_pct: float | None, resolved_total: int) -> str:
    """Render the accuracy line.

    Honest empty-state: ``None`` → "tracking your first resolutions"
    (the contract makes this field Optional precisely to avoid the
    "0.0%" lie when no call has resolved yet).
    """
    if accuracy_pct is None or resolved_total == 0:
        return "Accuracy: tracking your first resolutions"
    return f"Accuracy: {accuracy_pct:.0f}% ({resolved_total} resolved)"


def _direction_human(value: str) -> str:
    """``long`` → ``long``, ``short`` → ``short``. Lowercase for the inline format."""
    if value in {"long", "short"}:
        return value
    return value or "?"


def _horizon_human(code: str) -> str:
    """Translate wire codes to user-facing strings.

    Mirrors ``apps.bot.merlin_bot.handlers.alpha.step_preview._horizon_human``
    — kept as a private copy here so the /me handler doesn't import
    /alpha internals. The wire codes are stable (MVP §15 invariant 4);
    if a new horizon lands, both maps update together.
    """
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
    return mapping.get(code, code or "soon")


def _utc_datetime(iso: str) -> str:
    """Format an ISO timestamp as ``YYYY-MM-DD HH:MM UTC``.

    Same format as the /alpha receipt so the user sees identical
    timestamps in both places. Minute precision (no seconds — the
    resolver's window is ±5 minutes; seconds would be false
    precision).
    """
    if not iso:
        return "soon"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "soon"
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _not_onboarded_text() -> str:
    """User typed /me but never finished onboarding."""
    return "Hit /start first to set up your account."


def _temporary_error_text() -> str:
    """API down or unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /me again in a moment."
