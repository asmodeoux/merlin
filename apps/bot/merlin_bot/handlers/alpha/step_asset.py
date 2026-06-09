"""Asset picker step — search-driven (Q-027 lazy resolve).

User types a query (e.g. "ether"), bot calls ``GET /assets/search``,
renders the top-5 candidates with price + market cap and clickable
CoinGecko links so the user can verify before picking. On pick the bot
calls ``POST /assets/resolve`` to materialise the local row.

While on the picker the user can also:

* Send another ticker / name — re-renders the picker as a new message
  below the previous one (mirrors the ``receive_horizon_text`` pattern;
  the old picker stays clickable).
* Paste a CoinGecko token URL (e.g. ``coingecko.com/en/coins/stable-2``)
  — the bot extracts the slug, resolves it directly, and lands on the
  preview. A URL-shaped reply that fails to parse / resolve gets a
  format-hint reply with three example URLs and never silently falls
  through to a text search.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from merlin_bot.api_dependency import BOT_DATA_KEY
from merlin_bot.formatting import escape_md_v2
from merlin_bot.handlers.alpha.states import (
    USER_KEY_ASSET_RESOLUTION,
    USER_KEY_CANDIDATES,
    AlphaState,
)
from merlin_bot.handlers.alpha.step_preview import show_preview
from merlin_bot.handlers.alpha.ui import render as ui_render

_log = logging.getLogger("merlin.bot.handlers.alpha.asset")

CB_PREFIX = "alpha:asset:"

# Number-emoji prefixes for the picker. Fixed-size tuple — the picker
# renders at most ``_MAX_CANDIDATES`` rows so the body text and the
# button labels stay in lockstep with this sequence. Each entry is the
# three-codepoint Telegram keycap-emoji sequence (digit + VS16 +
# combining-keycap); writing the codepoints out explicitly side-steps
# the case where a source-file viewer collapses the VS16 and Telegram
# falls back to the plain ASCII digit.
_NUMBER_EMOJI: tuple[str, ...] = (
    "1\ufe0f\u20e3",
    "2\ufe0f\u20e3",
    "3\ufe0f\u20e3",
    "4\ufe0f\u20e3",
    "5\ufe0f\u20e3",
)

_MAX_CANDIDATES = 5

_COINGECKO_URL_TEMPLATE = "https://www.coingecko.com/en/coins/{slug}"

_PICKER_FOOTER = "Tap an option, send another ticker, or paste a CoinGecko token URL."

# Format-hint reply for URL-shaped text that fails to parse or resolve.
# Three concrete examples covering the shapes we accept: with scheme,
# without scheme, with locale prefix. Periods + slashes inside the
# URLs are not MarkdownV2-reserved here because we send this hint as
# plain text (no parse_mode) — keeps the example URLs copy-pasteable.
_URL_FORMAT_HINT = (
    "I couldn't read that as a CoinGecko token link. Expected one of these formats:\n"
    "\n"
    "https://www.coingecko.com/en/coins/bitcoin\n"
    "www.coingecko.com/coins/bitcoin\n"
    "coingecko.com/en/coins/bitcoin\n"
    "\n"
    "Or send a ticker / name to search again."
)

_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


async def prompt_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Morph the preview card into the asset picker or text prompt.

    If we already have candidates cached from the freeform LLM hop,
    show them as buttons + body; otherwise prompt the user to type.
    The text-prompt branch has no keyboard — the user's reply is the
    input and ``receive_asset_query`` sends the candidate keyboard
    as a new message.
    """
    query = update.callback_query
    if query is not None:
        await query.answer()

    candidates = context.user_data.get(USER_KEY_CANDIDATES, []) if context.user_data else []
    if candidates:
        await _render_picker(update, context, candidates)
        return AlphaState.PICK_ASSET

    await ui_render(
        update,
        context,
        text="Which asset? Type a name or ticker (BTC, ETH, RNDR, …).",
        reply_markup=None,
    )
    return AlphaState.EDIT_ASSET


async def receive_asset_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a search query; hit the API and render candidates."""
    if update.message is None or update.effective_chat is None:
        return ConversationHandler.END
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("Type the asset's name or ticker.")
        return AlphaState.EDIT_ASSET

    candidates = await _search(update, context, query=query)
    if candidates is None:
        return AlphaState.EDIT_ASSET
    if not candidates:
        await update.message.reply_text("No matches. Try a ticker like BTC or ETH, or a coin name.")
        return AlphaState.EDIT_ASSET

    if context.user_data is not None:
        context.user_data[USER_KEY_CANDIDATES] = candidates
    await _render_picker(update, context, candidates)
    return AlphaState.PICK_ASSET


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicked a candidate. Resolve it via the API; back to preview."""
    query = update.callback_query
    if query is None or context.user_data is None or update.effective_chat is None:
        return ConversationHandler.END
    await query.answer()
    coingecko_id = (query.data or "").removeprefix(CB_PREFIX)
    if not coingecko_id:
        return AlphaState.PICK_ASSET

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        resolution = await client.resolve_asset(coingecko_id=coingecko_id)
    except MerlinHTTPError as e:
        _log.warning("resolve_asset failed: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Couldn't lock that in — try picking again.",
        )
        return AlphaState.PICK_ASSET

    context.user_data[USER_KEY_ASSET_RESOLUTION] = resolution
    return await show_preview(update, context)


async def on_text_during_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent text while the picker was up — URL or fresh search.

    Branches on whether the text looks URL-shaped (case-insensitive
    substring ``coingecko.com``). URL-shaped text gates the URL path
    so a plain ticker like ``STABLE`` never accidentally goes through
    URL parsing.

    * **URL-shaped** → parse the slug; on success call
      ``client.resolve_asset`` and exit to the preview. On parse
      failure OR resolve failure (any ``MerlinHTTPError``), emit the
      URL-format hint with three example URLs and stay in
      ``PICK_ASSET``. We never silently fall through to a text search
      — the user explicitly tried a URL, so they get a clear answer
      about why it didn't take.
    * **Plain text** → re-render the picker as a new message below
      (matches ``step_horizon.receive_horizon_text``: text reply has
      no callback to edit, so ``ui_render`` sends a fresh message;
      old picker keyboard stays clickable).
    """
    if update.message is None or update.effective_chat is None:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        return AlphaState.PICK_ASSET

    if "coingecko.com" in text.lower():
        slug = _parse_coingecko_url(text)
        if slug is None:
            await update.message.reply_text(_URL_FORMAT_HINT)
            return AlphaState.PICK_ASSET
        client: MerlinClient = context.bot_data[BOT_DATA_KEY]
        try:
            resolution = await client.resolve_asset(coingecko_id=slug)
        except MerlinHTTPError as e:
            _log.info("resolve_asset for pasted URL slug %r failed: %s", slug, e)
            await update.message.reply_text(_URL_FORMAT_HINT)
            return AlphaState.PICK_ASSET
        if context.user_data is not None:
            context.user_data[USER_KEY_ASSET_RESOLUTION] = resolution
        return await show_preview(update, context)

    return await receive_asset_query(update, context)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


async def _search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str,
) -> list[dict[str, Any]] | None:
    """Run ``GET /assets/search`` and return the candidate list.

    Returns ``None`` if the API call failed (in which case the user
    already saw the "couldn't reach market data" message). Returns
    ``[]`` if the call succeeded but produced no candidates — the
    caller renders the "No matches" copy.
    """
    if update.message is None:
        return None
    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    try:
        result = await client.search_assets(query=query)
    except MerlinHTTPError as e:
        _log.warning("search_assets failed: %s", e)
        await update.message.reply_text("Couldn't reach market data — try again in a moment.")
        return None
    return list(result.get("candidates", []))


async def _render_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    candidates: list[dict[str, Any]],
) -> None:
    """Render the candidate list as MarkdownV2 body + numbered buttons.

    Body lines carry symbol (linked to CoinGecko), name, price, and
    market cap so look-alike rows (multiple "STABLE — Stable" coins)
    are visually distinguishable. Buttons keep the same number-emoji
    prefix so the click target stays unambiguous when the body has
    rendered already.

    ``disable_link_preview=True`` suppresses Telegram's auto-preview
    card for the first URL — without this the picker grows a
    CoinGecko thumbnail beneath every disambiguation prompt.
    """
    await ui_render(
        update,
        context,
        text=_render_candidates_text(candidates),
        reply_markup=_candidates_keyboard(candidates),
        parse_mode="MarkdownV2",
        disable_link_preview=True,
    )


def _render_candidates_text(candidates: list[dict[str, Any]]) -> str:
    """Build the MarkdownV2 body text for the picker.

    Format:

        Which one did you mean?
        1️⃣ [SYMBOL](url) — Name · $price · $cap cap
        2️⃣ ...

        Tap an option, send another ticker, or paste a CoinGecko token URL.

    Every dynamic substring is escaped via ``escape_md_v2`` because
    symbols / names from CoinGecko can carry any MarkdownV2-reserved
    character (parens, dots, brackets, etc.). The URL inside ``[…](…)``
    must NOT be escaped — Telegram's parser treats the URL slot as
    literal — but the slug component is restricted to
    ``[a-z0-9_-]`` upstream (see :func:`_parse_coingecko_url`'s same
    regex), so there are no reserved characters to escape there
    anyway.
    """
    lines: list[str] = ["Which one did you mean?"]
    for i, c in enumerate(candidates[:_MAX_CANDIDATES]):
        emoji = _NUMBER_EMOJI[i]
        symbol = str(c.get("symbol", "?"))
        name = str(c.get("name", "?"))
        cg_id = str(c.get("coingecko_id", ""))
        price = _format_money(c.get("current_price_usd"))
        cap = _format_market_cap(c.get("market_cap_usd"))
        url = _COINGECKO_URL_TEMPLATE.format(slug=cg_id)
        lines.append(
            f"{emoji} [{escape_md_v2(symbol)}]({url}) — {escape_md_v2(name)} "
            f"· {escape_md_v2(price)} · {escape_md_v2(cap)} cap"
        )
    lines.append("")
    lines.append(escape_md_v2(_PICKER_FOOTER))
    return "\n".join(lines)


def _candidates_keyboard(candidates: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Build the inline keyboard — one button per candidate, numbered.

    Button labels are ``"<emoji> SYMBOL — Name"`` (no price/cap;
    Telegram's button-label cap is ~64 chars and we don't want to
    truncate disambiguating context). The number emoji on each
    button matches the corresponding body line so users can map at
    a glance.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for i, c in enumerate(candidates[:_MAX_CANDIDATES]):
        emoji = _NUMBER_EMOJI[i]
        symbol = c.get("symbol", "?")
        name = c.get("name", "?")
        label = f"{emoji} {symbol} — {name}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_PREFIX}{c['coingecko_id']}")])
    return InlineKeyboardMarkup(rows)


def _coerce_float(value: object) -> float | None:
    """Best-effort coerce ``value`` to ``float``; ``None`` on failure.

    Centralises the "missing or unparseable" check so the price /
    market-cap formatters don't each carry their own copy. CoinGecko
    legitimately returns ``null`` for these on long-tail rows, and
    the upstream contract types both fields as ``float | None``;
    this helper also defends against a malformed string slipping
    through any future serialiser.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]  # narrowed at runtime by the except
    except (TypeError, ValueError):
        return None


def _format_money(value: object) -> str:
    """Format a USD price for the picker body.

    Four precision bands tuned for "what looks right next to a real
    coin price in chat":

    * ``>= $1`` → 2 decimals with thousands separators (``$1,234.56``).
    * ``>= $0.10`` → 2 decimals (``$0.87``, ``$0.21`` — the
      stablecoin / mid-cap zone the user's screenshot shows).
    * ``>= $0.001`` → 4 decimals (``$0.0094`` — sub-cent micro-caps
      where rounding to 2 dp would erase meaningful precision).
    * ``> $0`` → 6 significant digits (``$0.000123`` — meme-coin
      / dust territory).

    Missing or non-numeric values render as a single em-dash so the
    row still parses visually.
    """
    amount = _coerce_float(value)
    if amount is None:
        return "—"
    if amount == 0:
        return "$0.00"
    abs_amount = abs(amount)
    if abs_amount >= 1:
        result = f"${amount:,.2f}"
    elif abs_amount >= 0.1:
        result = f"${amount:.2f}"
    elif abs_amount >= 0.001:
        result = f"${amount:.4f}"
    else:
        result = f"${amount:.6g}"
    return result


def _format_market_cap(value: object) -> str:
    """Format a USD market cap as ``$1.2B`` / ``$5.2M`` / ``$200k`` / ``$123``.

    Three abbreviation tiers chosen to keep the picker body
    one-line-per-coin without sacrificing readability:

    * ``>= $1B`` → billions, one decimal place.
    * ``>= $1M`` → millions, one decimal place.
    * ``>= $1k`` → thousands, no decimals (avoids ``$200.0k``).
    * ``< $1k`` → integer dollars (long-tail dust coins).

    Missing / non-numeric / non-positive values render as an em-dash.
    """
    amount = _coerce_float(value)
    if amount is None or amount <= 0:
        return "—"
    if amount >= 1_000_000_000:
        result = f"${amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000:
        result = f"${amount / 1_000_000:.1f}M"
    elif amount >= 1_000:
        result = f"${amount / 1_000:.0f}k"
    else:
        result = f"${amount:.0f}"
    return result


def _parse_coingecko_url(text: str) -> str | None:
    """Extract a CoinGecko coin slug from a user-pasted URL, or ``None``.

    Accepts:

    * With or without ``http://`` / ``https://`` scheme.
    * Optional ``www.`` subdomain.
    * Any locale prefix (``/en/``, ``/ru/``, ``/zh-tw/``) or none at all.
    * Trailing path / query / fragment is ignored.

    Strategy:

    1. Quick substring check for ``coingecko.com`` (case-insensitive)
       — anything else is rejected upfront.
    2. Prepend ``https://`` if no scheme so ``urlparse`` returns a
       sane ``netloc``.
    3. Confirm the host ends in ``coingecko.com``.
    4. Walk path segments, find ``coins``, take the next segment as
       the slug.
    5. Validate the slug matches ``^[a-z0-9_-]+$`` — same character
       class CoinGecko itself uses for coin IDs, so a slug that
       passes this regex is a syntactically-valid candidate for
       ``POST /assets/resolve``.

    Returns ``None`` for any failure so the caller surfaces the
    format-hint reply.
    """
    t = text.strip()
    if "coingecko.com" not in t.lower():
        return None
    if not t.lower().startswith(("http://", "https://")):
        t = "https://" + t
    try:
        parsed = urlparse(t)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if not (host == "coingecko.com" or host.endswith(".coingecko.com")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    try:
        idx = parts.index("coins")
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    slug = parts[idx + 1].lower()
    return slug if _SLUG_RE.fullmatch(slug) else None
