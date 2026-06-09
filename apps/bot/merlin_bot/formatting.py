r"""Shared bot rendering helpers — MVP §12.10 display rules.

Sprint 1 fills in concrete formatters (claim card, board entry, redemption
receipt). The rules below are the contract every formatter must follow:

MVP §12.10 display rules:

- USD amounts: 2-decimal places (`$1,234.56`), thousands separators on.
- Returns: 4-decimal places (`+0.1234`), explicit `+`/`-` prefix.
- No scientific notation anywhere.
- Timestamps: ISO 8601 in UTC (`2026-01-15T13:42:00Z`) on the wire;
  user-facing surfaces use ``format_utc_datetime`` to render the same
  moment as ``"June 12 @ 16:32 UTC"`` (AGENTS.md bot-voice rule
  "no ISO timestamps in user-visible text").
- Canonical symbols (`BTC`, not `Bitcoin`); aliases resolved via
  `merlin_core.contracts.AssetAlias`.
- Every claim reference includes the claim_id (`#a1b2c3d4`).
- MarkdownV2-escaped output — every dynamic value goes through
  `escape_md_v2`.

PII (MVP §15 invariant 9):

- Telegram handles are NEVER rendered. Display the source_id
  (`tg:<hex>`) or a derived display name; the raw `@handle` lives only
  in the PII table.
- `merlin_bot.error_handler` strips Telegram-supplied PII before
  forwarding to Sentry.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Per the Telegram Bot API docs, these characters MUST be escaped in
# MarkdownV2 body text outside code/pre blocks.
MARKDOWN_V2_RESERVED = set("_*[]()~`>#+-=|{}.!\\")


def escape_md_v2(text: str) -> str:
    """Escape every MarkdownV2-reserved character with a backslash.

    Use this on every dynamic value before splicing into a MarkdownV2
    template. Never construct MarkdownV2 with f-strings that bypass this
    helper.
    """
    return "".join(f"\\{ch}" if ch in MARKDOWN_V2_RESERVED else ch for ch in text)


def format_utc_datetime(iso: str, *, fallback: str = "soon") -> str:
    """Format an ISO timestamp as ``"June 12 @ 16:32 UTC"``.

    Human shape, not computerish — full month name, day without
    leading zero, ``@`` separator before the wall-clock time, UTC
    suffix. Minute precision (no seconds — the resolver's window is
    ±5 minutes; seconds would be false precision). Always normalised
    to UTC; if the timestamp comes in with a non-UTC offset, the
    walltime is converted, not preserved.

    Used in three places today (the /alpha receipt's ``submitted_at``
    + ``resolves_at``, the /me handler's open-call lines, and the
    /invite reply's expiry) so every user-facing timestamp shares one
    shape — the contract for "this is when something happens" reads
    identically everywhere.

    On a missing / unparseable value returns ``fallback`` ("soon" by
    default) so a bot reply never crashes on a malformed server
    timestamp. Callers that want a different fallback word (e.g.
    "never") pass it explicitly.
    """
    if not iso:
        return fallback
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return f"{dt:%B} {dt.day} @ {dt:%H:%M} UTC"
