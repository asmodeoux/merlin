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
          ETH long — 2 weeks, entry $2,438.12, resolves 2026-06-12 16:35 UTC
          BTC long — 1 month, resolves 2026-06-29 09:10 UTC

    The entry-price chunk ("entry $X.YY") is shown only when the
    server has a captured snapshot (``entry_price_usd != None``). On
    legacy rows or rows submitted while CoinGecko was down we just
    omit the chunk — the bot never invents a placeholder, never
    shows "$0.00".

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
    """One open-call bullet line.

    Inline shape: ``ASSET direction — horizon, [entry $X.YY,] resolves <utc>``.
    The entry-price chunk only appears when the server captured a
    snapshot at submit time — see ``_format_entry_price`` for the
    adaptive decimal logic.
    """
    asset = str(call.get("asset_symbol") or "?").upper()
    direction = _direction_human(str(call.get("direction") or ""))
    horizon = _horizon_human(call.get("horizon_seconds"))
    resolves_at = _utc_datetime(str(call.get("resolves_at") or ""))
    entry_chunk = _format_entry_price(call.get("entry_price_usd"))
    middle = f"{horizon}, {entry_chunk}resolves {resolves_at}"
    return f"  {asset} {direction} — {middle}"


def _format_entry_price(price: object) -> str:
    """Return ``"entry $X.YY, "`` when the snapshot is present, else ``""``.

    Returns the trailing ``", "`` (or empty string) so the caller can
    inline this directly without conditional separators. Keeps the
    bullet-line composition declarative.

    Adaptive decimals: 2dp for >= $1 (BTC / ETH territory), 4dp for
    $0.01 - $1 (mid-cap alts), 8dp below (sub-cent memecoins). The
    threshold matches ``step_preview._format_usd`` — same rules on
    receipt + ``/me`` so a user reading their open list sees the
    identical number they saw at Confirm time.
    """
    if price is None:
        return ""
    try:
        amount = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if amount >= 1.0:
        rendered = f"${amount:,.2f}"
    elif amount >= 0.01:
        rendered = f"${amount:.4f}"
    else:
        rendered = f"${amount:.8f}"
    return f"entry {rendered}, "


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


def _horizon_human(value: object) -> str:
    """Render horizon (integer seconds) as a user-facing string.

    The wire format is integer seconds via
    ``OpenCallEntry.horizon_seconds``; ``humanise_horizon`` picks the
    cleanest unit (1 hour / 3 hours / 24 hours / 1 week / 30 days /
    2 months / 3 months). Returns ``"soon"`` as a defensive fallback
    for the edge case where ``horizon_seconds`` is missing / non-
    numeric - keeps the bullet line readable even with malformed
    server output.
    """
    from merlin_core.contracts.horizons import humanise_horizon

    if value is None or value == "":
        return "soon"
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "soon"
    if seconds <= 0:
        return "soon"
    return humanise_horizon(seconds)


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
