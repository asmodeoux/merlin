r"""MarkdownV2-safe rendering helpers — MVP §12.10 display rules.

Sprint 1 fills in concrete formatters (claim card, board entry, redemption
receipt). The rules below are the contract every formatter must follow:

MVP §12.10 display rules:

- USD amounts: 2-decimal places (`$1,234.56`), thousands separators on.
- Returns: 4-decimal places (`+0.1234`), explicit `+`/`-` prefix.
- No scientific notation anywhere.
- Timestamps: ISO 8601 in UTC (`2026-01-15T13:42:00Z`).
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
