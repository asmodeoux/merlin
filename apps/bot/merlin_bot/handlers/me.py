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

from merlin_bot import rich
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

    await rich.send_rich(
        context.bot,
        update.message.chat_id,
        _render_me_rich(payload),
        fallback_text=_render_me(payload),
    )


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

        Open: 3   Resolved: 1
        PnL -1.30%  ·  0% hit  ·  1 call

        Open calls:
          ETH long — 2 weeks, entry $2,438.12, resolves June 12 @ 16:35 UTC
          BTC long — 1 month, resolves June 29 @ 09:10 UTC

    The second headline line carries PnL primary (matching the locked
    ``primary_metric = pnl_primary`` choice — same metric prominence
    as /board) and hit rate as a secondary chunk. Cold-start collapses
    to "Accuracy: tracking your first resolutions" — never a
    misleading "+0.00% PnL" before anything has resolved.

    The entry-price chunk ("entry $X.YY") on each open-call line is
    shown only when the server has a captured snapshot
    (``entry_price_usd != None``). On legacy rows or rows submitted
    while CoinGecko was down we just omit the chunk — the bot never
    invents a placeholder, never shows "$0.00".

    Zero-open empty-state collapses the list section to a single
    "No open calls yet" line so the message stays short.
    """
    display_name = payload.get("display_name") or "you"
    open_calls = payload.get("calls_open") or []
    resolved_total = int(payload.get("calls_resolved_total") or 0)
    headline_stats = _headline_stats_line(
        weighted_pnl_pct=payload.get("weighted_pnl_pct"),
        accuracy_pct=payload.get("accuracy_pct"),
        resolved_total=resolved_total,
    )

    header = f"{display_name} — your calls"
    counts = f"Open: {len(open_calls)}   Resolved: {resolved_total}"

    if open_calls:
        rendered_calls = "\n".join(_render_open_call(c) for c in open_calls)
        body = "Open calls:\n" + rendered_calls
    else:
        body = "No open calls yet. /alpha to make your first."

    footer = "/alpha to add another, /board for the leaderboard."
    look_back = "Tap /tickers for your per-ticker breakdown, /history for past calls."

    return "\n\n".join([header, f"{counts}\n{headline_stats}", body, footer, look_back])


def _render_me_rich(payload: dict[str, Any]) -> str:
    """Compose the ``/me`` reply as a Rich Message (Bot API 10.1).

    ``/me`` is a personal profile, not a leaderboard, so it stays
    structured rather than collapsing into one big table:

    * ``# <name> — your calls`` heading.
    * A bold "stat strip": ``**Open** N · **Resolved** M`` then the
      headline PnL/hit/resolved line (or the cold-start tracking copy).
    * ``## Open calls`` + a five-column table. The **Resolves** column
      uses a native ``tg://time`` entity (:func:`merlin_bot.rich.tg_time`)
      so each open call shows a live, client-localised countdown that
      ticks down on the user's device — the standout upgrade over the
      static "resolves June 12 @ 16:35 UTC" string (which is preserved
      as the entity's fallback label for clients that can't render it).

    Cold-start / zero-open states keep the plain renderer's honest copy:
    "No open calls yet" rather than an empty table, and "tracking your
    first resolutions" rather than a faked +0.00%.
    """
    display_name = str(payload.get("display_name") or "you")
    open_calls = payload.get("calls_open") or []
    resolved_total = int(payload.get("calls_resolved_total") or 0)

    blocks = [
        f"# {rich.escape_inline(display_name)} — your calls",
        "",
        f"**Open** {len(open_calls)} · **Resolved** {resolved_total}",
        # Blank line between the count line and the headline stats:
        # Telegram soft-joins single-newline lines into one paragraph,
        # so without it "Resolved 8" and "PnL …" run together.
        "",
        _headline_stats_rich(
            weighted_pnl_pct=payload.get("weighted_pnl_pct"),
            accuracy_pct=payload.get("accuracy_pct"),
            resolved_total=resolved_total,
        ),
        "",
    ]

    if open_calls:
        # ``Call`` folds asset + direction ("GOLD long") into one column
        # to save horizontal space; ``Resolves`` sits before ``Entry``
        # since the deadline matters more at a glance than the entry price.
        table = rich.table(
            ["Call", "Horizon", "Resolves", "Entry"],
            [_rich_open_call(call) for call in open_calls],
            ["left", "left", "left", "right"],
        )
        blocks.extend(["## Open calls", "", table, ""])
    else:
        blocks.extend(["No open calls yet. /alpha to make your first.", ""])

    blocks.extend(
        [
            "/alpha to add another, /board for the leaderboard.",
            "",
            "Tap /tickers for your per-ticker breakdown, /history for past calls.",
        ],
    )
    return "\n".join(blocks)


def _headline_stats_rich(
    *,
    weighted_pnl_pct: object,
    accuracy_pct: object,
    resolved_total: int,
) -> str:
    """Bold stat strip for the rich ``/me`` header.

    Same two shapes (and same cold-start gating) as
    :func:`_headline_stats_line`, restyled for Rich Markdown: an italic
    "tracking your first resolutions" line on cold-start, or a
    ``**PnL** +1.85% · **Hit** 60% · **5 calls**`` strip once outcomes
    land. PnL stays primary with two decimals to match /board.
    """
    if weighted_pnl_pct is None or accuracy_pct is None or resolved_total == 0:
        return "_Accuracy: tracking your first resolutions_"
    try:
        pnl = float(weighted_pnl_pct)  # type: ignore[arg-type]
        acc = float(accuracy_pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "_Accuracy: tracking your first resolutions_"
    calls_suffix = "" if resolved_total == 1 else "s"
    return f"**PnL** {pnl:+.2f}% · **Hit** {acc:.0f}% · **{resolved_total} call{calls_suffix}**"


def _rich_open_call(call: dict[str, Any]) -> list[str]:
    """One open-call table row ``[call, horizon, resolves, entry]``.

    Mirrors :func:`_render_open_call` field-for-field. ``call`` folds the
    asset symbol + direction into one cell ("GOLD long"); the resolves
    column is a live ``tg://time`` countdown shown before the entry
    column; the entry column reuses the adaptive-decimal USD format.
    """
    asset = rich.escape_cell(str(call.get("asset_symbol") or "?").upper())
    direction = rich.escape_cell(_direction_human(str(call.get("direction") or "")))
    horizon = rich.escape_cell(_horizon_human(call.get("horizon_seconds")))
    return [
        f"{asset} {direction}",
        horizon,
        _resolves_cell(str(call.get("resolves_at") or "")),
        _entry_cell(call.get("entry_price_usd")),
    ]


def _entry_cell(price: object) -> str:
    """Adaptive-decimal USD entry price for a table cell; ``—`` if absent.

    Same bucket thresholds as :func:`_format_entry_price` (>=$1 -> 2dp,
    $0.01-$1 -> 4dp, <$0.01 -> 8dp) so the number matches the /alpha
    receipt and the plain /me line — but rendered as a bare ``$X`` cell
    (no "entry " prefix; the column header carries the label) and an
    em-dash when the server captured no snapshot.
    """
    if price is None:
        return "—"
    try:
        amount = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if amount >= 1.0:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.4f}"
    return f"${amount:.8f}"


def _resolves_cell(iso: str) -> str:
    """Render the resolves-at cell as a live ``tg://time`` countdown.

    The entity renders as a relative countdown ("in 3 hours") that
    updates client-side; its fallback label is the same absolute UTC
    string the plain renderer shows ("June 12 @ 16:35 UTC") so clients
    that can't render the entity still get a readable cell. Falls back
    to ``"soon"`` on a missing / malformed timestamp.
    """
    if not iso:
        return "soon"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "soon"
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    label = f"{dt:%B} {dt.day} @ {dt:%H:%M} UTC"
    return rich.tg_time(dt, fmt="r", label=label)


#: Prefix for each open-call line. ``•`` (U+2022 BULLET) instead of
#: two spaces — the spaces left the list visually flat against the
#: "Open calls:" header, especially in the Telegram font, so users
#: couldn't tell where one row ended and the next began. A real bullet
#: glyph reads as a list at a glance and stays terminal-friendly
#: (no parse_mode escaping needed; not a Markdown special char).
_OPEN_CALL_BULLET = "•"


def _render_open_call(call: dict[str, Any]) -> str:
    """One open-call bullet line.

    Inline shape:
        ``• ASSET direction — horizon, [entry $X.YY,] resolves <chunk>``

    ``<chunk>`` is the resolves-at fragment from ``_resolves_at_chunk``:
    relative ("in 2 hours 11 minutes") when the deadline is < 24h
    away, absolute ("June 12 @ 16:35 UTC") otherwise. The entry-price
    chunk only appears when the server captured a snapshot at submit
    time — see ``_format_entry_price`` for the adaptive decimal logic.
    """
    asset = str(call.get("asset_symbol") or "?").upper()
    direction = _direction_human(str(call.get("direction") or ""))
    horizon = _horizon_human(call.get("horizon_seconds"))
    resolves_chunk = _resolves_at_chunk(str(call.get("resolves_at") or ""))
    entry_chunk = _format_entry_price(call.get("entry_price_usd"))
    middle = f"{horizon}, {entry_chunk}{resolves_chunk}"
    return f"{_OPEN_CALL_BULLET} {asset} {direction} — {middle}"


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


def _headline_stats_line(
    *,
    weighted_pnl_pct: object,
    accuracy_pct: object,
    resolved_total: int,
) -> str:
    """Render the headline-stats line under the Open/Resolved counts.

    Two shapes:

    * Cold-start (no resolutions yet, or no PnL snapshot): a single
      "Accuracy: tracking your first resolutions" line — the contract
      makes both ``accuracy_pct`` and ``weighted_pnl_pct`` Optional
      precisely so we can render this honestly rather than faking a
      ``+0.00% PnL`` / ``0.0% hit``.

    * Resolved-state: PnL primary + hit rate secondary + call count,
      separated by middle-dots. ``PnL -1.30%  ·  0% hit  ·  1 call``
      — same chunk order as the /board row so users see consistent
      numbers in both views. ``calls`` pluralises ("1 call" /
      "8 calls").

    Defensive: if PnL is missing but accuracy is present (impossible
    via the current DB CHECKs but pinning for forward-compat) we fall
    back to the cold-start line. PnL is the headline; without it the
    row would feel half-finished.
    """
    if weighted_pnl_pct is None or accuracy_pct is None or resolved_total == 0:
        return "Accuracy: tracking your first resolutions"
    try:
        pnl = float(weighted_pnl_pct)  # type: ignore[arg-type]
        acc = float(accuracy_pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Accuracy: tracking your first resolutions"
    calls_suffix = "" if resolved_total == 1 else "s"
    return f"PnL {pnl:+.2f}%  ·  {acc:.0f}% hit  ·  {resolved_total} call{calls_suffix}"


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
    """Format an ISO timestamp as ``June 12 @ 21:43 UTC``.

    Thin wrapper around ``merlin_bot.formatting.format_utc_datetime``
    so the /me lines share one shape with the /alpha receipt and the
    /invite expiry.
    """
    from merlin_bot.formatting import format_utc_datetime

    return format_utc_datetime(iso)


# A bet expiring within this many seconds renders as a relative
# countdown ("in 2 hours 11 minutes") rather than an absolute UTC
# timestamp. 24 hours keeps the short-horizon UX scannable without
# trying to compute "in 3 days 4 hours" — past a day, an absolute
# date is easier to plan against.
_RELATIVE_RESOLVES_AT_SECONDS = 24 * 60 * 60


def _resolves_at_chunk(iso: str, *, now: datetime | None = None) -> str:
    """Render the resolves-at fragment for an open-call bullet line.

    Two shapes, gated by ``_RELATIVE_RESOLVES_AT_SECONDS``:

    * ``< 24h, > 0``: ``"resolves in 2 hours 11 minutes"`` — friendlier
      to scan when the deadline is imminent.
    * ``>= 24h`` (or overdue / unparseable): ``"resolves June 12 @
      16:35 UTC"`` — the same human UTC format as the /alpha
      receipt. Overdue rows are a transient pre-resolution state; we
      keep showing the absolute time rather than "in -5 minutes".

    ``now`` is injectable for tests so the relative-branch assertions
    aren't flaky against wall-clock drift. Production passes ``None``
    and gets ``datetime.now(UTC)``.
    """
    if not iso:
        return "resolves soon"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "resolves soon"
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    seconds_to_go = int((dt - current).total_seconds())
    if 0 < seconds_to_go < _RELATIVE_RESOLVES_AT_SECONDS:
        return f"resolves in {_humanise_seconds_left(seconds_to_go)}"
    return f"resolves {dt:%B} {dt.day} @ {dt:%H:%M} UTC"


def _humanise_seconds_left(seconds: int) -> str:
    """Render a positive ``< 24h`` countdown as ``"X hours Y minutes"``.

    Suppress zero components and apply singular/plural ("1 hour 1
    minute" / "2 hours 11 minutes" / "45 minutes" / "1 hour"). Anything
    below a minute degrades to ``"less than a minute"`` rather than
    ``"0 minutes"`` so the bot never shows a misleading zero.

    Stays in hours+minutes (no seconds) — the resolver's price window
    is ±5 minutes, so sub-minute precision in the countdown would
    overpromise.
    """
    minutes_total = seconds // 60
    hours, minutes = divmod(minutes_total, 60)
    if hours and minutes:
        return f"{hours} {_pluralise(hours, 'hour')} {minutes} {_pluralise(minutes, 'minute')}"
    if hours:
        return f"{hours} {_pluralise(hours, 'hour')}"
    if minutes:
        return f"{minutes} {_pluralise(minutes, 'minute')}"
    return "less than a minute"


def _pluralise(n: int, noun: str) -> str:
    """Naive English plural for the small set we need (``hour`` / ``minute``)."""
    return noun if n == 1 else f"{noun}s"


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _not_onboarded_text() -> str:
    """User typed /me but never finished onboarding."""
    return "Hit /start first to set up your account."


def _temporary_error_text() -> str:
    """API down or unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /me again in a moment."
