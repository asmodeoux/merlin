"""``/board`` handler — the leaderboard.

Stateless. Hits ``GET /board?window=all_time``; renders the top-N as
emoji-prefixed lines (medals for the top three, plain rank numbers
below). The caller's own row is bolded so they can find themselves at
a glance in a packed list.

Self-positioning rules:

* If the caller's rank is inside the visible top-N → their row is
  bolded inline (no extra footer).
* If the caller has resolved calls but ranks outside the top-N → an
  "…" gap line followed by their row is appended so they always see
  themselves on the board.
* If the caller has zero resolved calls → no row appendage; we print
  an inspiring one-liner instead. They're not on the board yet, and
  showing rank "—" or skipping silently both feel worse than telling
  them what unlocks a spot.

Bot-voice rules (plan §6):

* No "v0" / "Phase 2" / "coming soon" markers. The empty-state reads
  "Leaderboard unlocks after the first resolutions." — present tense,
  product framing.
* No internal scoring nouns ("Bayesian", "shrinkage", "regret",
  "belief score") on the rendered string, even in the populated path.
* HTML parse mode — Telegram's ``<b>...</b>`` is the only formatting
  we use (to bold the caller's row). HTML is far gentler on
  user-controlled text than MarkdownV2: we only need to escape the
  five HTML special chars in the display_name + numeric fields rather
  than every MarkdownV2 punctuation mark.

We also fetch ``GET /me`` in parallel with ``GET /board`` to learn the
caller's display_name + resolved-count. The display_name powers self-
recognition (bold + appendage match); the count distinguishes
"cold-start, show the inspiring nudge" from "has resolutions, must be
somewhere in the data". If /me fails the board still renders — just
without self-highlight or appendage. /board is the must-have, /me is
best-effort.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from merlin_client import MerlinClient
from merlin_client.errors import MerlinHTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from merlin_bot import rich
from merlin_bot.api_dependency import BOT_DATA_KEY

_log = logging.getLogger("merlin.bot.handlers.board")

# How many rows we show on the rendered board. The query may return
# more; we trim client-side to keep the message short. 30 is a
# deliberate balance — long enough that the typical caller is on the
# board without scrolling tricks, short enough that the message fits
# comfortably under Telegram's 4096-char limit even with HTML markup
# (30 * ~80 chars = ~2.4 KB, well clear).
LEADERBOARD_LIMIT: int = 30

# Emoji prefixes for the top three ranks. Anything below the podium
# gets the rank number rendered as plain text (4., 5., ...) so the
# visual hierarchy stays obvious without making the bottom of the
# board feel like an afterthought.
_PODIUM_EMOJI: dict[int, str] = {1: "🥇", 2: "🥈", 3: "🥉"}

# Gap separator between the visible top-N and the caller's row when
# the caller ranks outside the top-N. A single horizontal-ellipsis
# glyph (U+2026), used standalone on its own line — Telegram renders
# it identically across clients and it doesn't need HTML-escaping.
_GAP_LINE = "…"

# Horizon-filter inline buttons. Four callback-data constants for the
# four modes; the regex pattern gates the CallbackQueryHandler so
# unrelated callbacks never reach ``on_filter_callback``. Plain
# prefix-string callback_data per the in-tree convention (alpha steps,
# history pagination) — typed CallbackData would require enabling
# ``arbitrary_callback_data`` on the ApplicationBuilder, out of scope.
CB_BOARD_FILTER_ALL = "board:filter:all"
CB_BOARD_FILTER_DAY = "board:filter:day"
CB_BOARD_FILTER_SWING = "board:filter:swing"
CB_BOARD_FILTER_POSITION = "board:filter:position"
_BOARD_FILTER_CB_PREFIX = "board:filter:"
_BOARD_FILTER_PATTERN = r"^board:filter:(all|day|swing|position)$"

# Four-mode filter the bot exposes. The wire value (``"all"`` /
# ``"day"`` / ``"swing"`` / ``"position"``) is what we pass to
# ``client.get_board`` and what comes back on ``BoardResponse.horizon``.
# Mirrored here as a frozenset for in-handler validation;
# ``contracts.board.HorizonFilter`` is the canonical Literal but lives
# behind the contract import.
_VALID_FILTERS: frozenset[str] = frozenset({"all", "day", "swing", "position"})

# Stable display order for filter buttons + empty-state nudges. ``all``
# first when it's a non-active option, then day/swing/position in
# increasing-horizon order. Defined once so the keyboard layout and
# the empty-state "tap X, Y, or Z" copy stay in lock-step.
_FILTER_ORDER: tuple[str, ...] = ("all", "day", "swing", "position")

# Human labels for each filter — used in both the inline-button text
# and the rendered message-header suffix. The "≤24h" / "1d-3wk" /
# ">3wk" disclosure on the header keeps the segmentation honest (the
# button itself stays short; the header line carries the precise
# threshold).
_FILTER_BUTTON_LABEL: dict[str, str] = {
    "all": "All",
    "day": "Day Traders",
    "swing": "Swing Traders",
    "position": "Position Traders",
}
_FILTER_HEADER_SUFFIX: dict[str, str] = {
    "all": "",
    "day": " · Day Traders (≤24h)",
    "swing": " · Swing Traders (1d-3wk)",
    "position": " · Position Traders (>3wk)",
}
_FILTER_CALLBACK_DATA: dict[str, str] = {
    "all": CB_BOARD_FILTER_ALL,
    "day": CB_BOARD_FILTER_DAY,
    "swing": CB_BOARD_FILTER_SWING,
    "position": CB_BOARD_FILTER_POSITION,
}

# Substring Telegram returns when ``edit_message_text`` is called with
# the same content as the existing message. Tapping the currently-active
# filter button is technically impossible (we hide it), but a fast
# double-tap on a different filter can race into this state — swallow
# it silently rather than letting the spinner hang.
_NOT_MODIFIED_SUBSTRING = "message is not modified"


async def board_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``GET /board`` (+ ``GET /me``) and reply with the rendered top-N.

    /board is the must-have; /me is best-effort because we only use it
    to recognise "this row is the caller, bold it". If /me errors,
    the leaderboard still renders — just without self-highlight.

    Initial render is unfiltered (``horizon="all"``); the user toggles
    between Day Traders / Swing Traders / Position Traders / All via
    the inline-button column attached below the message. The
    three-of-four button rule (active is hidden) and the vertical-stack
    layout live in ``_filter_keyboard``.
    """
    if update.message is None or update.effective_user is None:
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    tg_user_id = update.effective_user.id

    rich_text, fallback_text, keyboard = await _fetch_and_render(
        client,
        tg_user_id=tg_user_id,
        active_filter="all",
    )
    if rich_text is None or fallback_text is None:
        await update.message.reply_text(_temporary_error_text())
        return

    await rich.send_rich(
        context.bot,
        update.message.chat_id,
        rich_text,
        reply_markup=keyboard,
        fallback_text=fallback_text,
        fallback_parse_mode=ParseMode.HTML,
    )


async def on_filter_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Re-render ``/board`` in place for the tapped horizon filter.

    The regex ``pattern`` on the registered ``CallbackQueryHandler``
    guarantees ``query.data`` matches
    ``board:filter:(all|day|swing|position)``; we defensively validate
    anyway so a future regex bug fails closed rather than silently
    passing an unknown value to the API.

    Mirrors the edit-in-place semantics of
    ``handlers/alpha/ui.py::render`` and ``handlers/history.py::
    on_page_callback``: ``query.edit_message_text`` with a
    "message is not modified" swallow + a ``send_message`` fallback
    when the original message is too old to edit (Telegram drops
    edit rights after 48h).
    """
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return

    new_filter = query.data.removeprefix(_BOARD_FILTER_CB_PREFIX)
    if new_filter not in _VALID_FILTERS:
        _log.warning("board filter callback got unexpected data: %r", query.data)
        # Still clear the spinner so the button doesn't hang for 30s.
        await query.answer()
        return

    client: MerlinClient = context.bot_data[BOT_DATA_KEY]
    tg_user_id = update.effective_user.id

    rich_text, fallback_text, keyboard = await _fetch_and_render(
        client,
        tg_user_id=tg_user_id,
        active_filter=new_filter,
    )
    if rich_text is None or fallback_text is None:
        # Re-fetch failed — leave the existing message intact rather
        # than wiping it for a temporary outage. The toast is the
        # ONLY ``query.answer`` call on this path: Telegram only
        # honours one ``answerCallbackQuery`` per query, and a
        # bare ``query.answer()`` earlier would silently swallow
        # the toast we want the user to see.
        await query.answer(
            text="Couldn't refresh — try again in a moment.",
            show_alert=False,
        )
        return

    # Clear the spinner now that we have something to render. Done
    # before the edit so the spinner clears even if the edit raises
    # (the user's tap should always feel responsive).
    await query.answer()

    # ``edit_rich`` needs an explicit chat_id + message_id (the raw Bot
    # API call can't infer them the way ``query.edit_message_text``
    # does). Both come off the callback's own message; if it's gone
    # (user deleted it) there's nothing to edit and nothing to do.
    chat_id = getattr(query.message, "chat_id", None)
    message_id = getattr(query.message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    try:
        await rich.edit_rich(
            context.bot,
            chat_id=chat_id,
            message_id=message_id,
            markdown=rich_text,
            reply_markup=keyboard,
        )
    except BadRequest as e:
        if _NOT_MODIFIED_SUBSTRING in str(e).lower():
            return
        _log.warning(
            "board filter edit_rich fell back to send_rich: %s",
            e,
        )
        await rich.send_rich(
            context.bot,
            chat_id,
            rich_text,
            reply_markup=keyboard,
            fallback_text=fallback_text,
            fallback_parse_mode=ParseMode.HTML,
        )


def build_handler() -> CommandHandler:
    """Wire the ``/board`` CommandHandler."""
    return CommandHandler("board", board_command)


def build_callback_handler() -> CallbackQueryHandler:
    """Wire the inline-button handler for the horizon-filter row."""
    return CallbackQueryHandler(on_filter_callback, pattern=_BOARD_FILTER_PATTERN)


async def _fetch_and_render(
    client: MerlinClient,
    *,
    tg_user_id: int,
    active_filter: str,
) -> tuple[str | None, str | None, InlineKeyboardMarkup | None]:
    """Run the parallel ``/board`` + ``/me`` fetch and render the reply.

    Returns ``(None, None, None)`` on a board-fetch error (the caller
    renders a temporary-error message); a populated
    ``(rich_text, fallback_text, keyboard)`` triple on success —
    including the empty-state path. ``rich_text`` is the Bot API 10.1
    Rich Message body; ``fallback_text`` is the legacy HTML render the
    sender drops back to if Telegram rejects the rich payload. The
    keyboard is always the three non-active filter buttons so users can
    switch filters even when the active slice is empty.
    """
    board_task = asyncio.create_task(
        client.get_board(window="all_time", horizon=active_filter),
    )
    me_task = asyncio.create_task(client.get_me(tg_user_id=tg_user_id))

    try:
        board_payload = await board_task
    except MerlinHTTPError as e:
        _log.warning("get_board failed: %s", e)
        me_task.cancel()
        return None, None, None
    except Exception as e:
        _log.warning("get_board transport error: %s", e)
        me_task.cancel()
        return None, None, None

    self_display_name: str | None = None
    self_resolved_total: int = 0
    try:
        me_payload = await me_task
    except Exception as e:
        # /me failures (404, transport, etc.) degrade gracefully:
        # leaderboard renders without bolding the caller's row and
        # without the cold-start / outside-top-N appendage.
        _log.debug("get_me for /board self-highlight failed: %s", e)
    else:
        self_display_name = me_payload.get("display_name")
        # Defensive int-cast: server contract guarantees an int, but a
        # mid-deploy stale schema (None / str) shouldn't crash /board.
        try:
            self_resolved_total = int(me_payload.get("calls_resolved_total") or 0)
        except (TypeError, ValueError):
            self_resolved_total = 0

    # Trust the server's echo over the requested filter so the
    # rendered header always matches the slice the user is seeing —
    # a stale-deploy mismatch shows the truth, not the request.
    rendered_filter = _coerce_filter(board_payload.get("horizon"), active_filter)

    rich_text = _render_board_rich(
        board_payload,
        self_display_name=self_display_name,
        self_resolved_total=self_resolved_total,
        active_filter=rendered_filter,
    )
    fallback_text = _render_board(
        board_payload,
        self_display_name=self_display_name,
        self_resolved_total=self_resolved_total,
        active_filter=rendered_filter,
    )
    keyboard = _filter_keyboard(rendered_filter)
    return rich_text, fallback_text, keyboard


def _coerce_filter(echoed: object, fallback: str) -> str:
    """Coerce a wire ``horizon`` echo to one of the three valid modes.

    Defensive: ``BoardResponse.horizon`` is typed as a ``Literal`` on
    the server, but a mid-deploy old API could omit the field. We
    fall back to the requested filter when the server's echo is
    missing or unknown so the rendered header stays sensible.
    """
    if isinstance(echoed, str) and echoed in _VALID_FILTERS:
        return echoed
    return fallback if fallback in _VALID_FILTERS else "all"


def _filter_keyboard(active_filter: str) -> InlineKeyboardMarkup:
    """Build the three-button vertical column for the horizon filter.

    Per the "4 modes, 3 buttons" rule: the active filter is implicit
    in the message header text, so the inline keyboard shows the three
    *non-active* filters. Every tap therefore switches to a different
    view — no "tap the selected one and nothing happens" UX surprise.

    Buttons stack vertically (one per row) rather than horizontally:
    the "Position Traders" label is wide enough that three side-by-side
    buttons overflow on narrow mobile clients (Telegram doesn't wrap
    inline-keyboard rows for us — it just truncates), so a one-button-
    per-row column is the only layout that guarantees the labels stay
    fully readable across every client width.

    Order is stable (``all`` first when non-active, then ``day``,
    ``swing``, ``position`` in increasing-horizon order — the
    ``_FILTER_ORDER`` constant) so the buttons don't jump around
    between renders.
    """
    rows = [
        [
            InlineKeyboardButton(
                _FILTER_BUTTON_LABEL[mode],
                callback_data=_FILTER_CALLBACK_DATA[mode],
            ),
        ]
        for mode in _FILTER_ORDER
        if mode != active_filter
    ]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_board_rich(
    payload: dict[str, Any],
    *,
    self_display_name: str | None = None,
    self_resolved_total: int = 0,
    active_filter: str = "all",
) -> str:
    """Compose the ``/board`` reply as a Rich Message (Bot API 10.1).

    Same data, ranking, self-positioning, and filter semantics as
    :func:`_render_board`; the emoji-prefixed line list becomes a native
    five-column table (``#`` / ``Caller`` / ``PnL`` / ``Hit`` /
    ``Calls``) so the numbers line up in right-aligned columns instead
    of running inline behind middle-dots.

    Self-positioning carries over: the caller's name is wrapped in
    ``**bold**`` (Markdown's table-cell equivalent of the old ``<b>``),
    and an outside-top-N caller still gets a ``…`` gap row followed by
    their own row appended to the table. The cold-start inspiring line
    and the filtered/empty states reuse the plain renderer's honest copy
    (no table to align). The footer ranking explainer + activity
    heartbeat is rendered as an italic block; the CTA follows.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text(active_filter=active_filter)

    top_entries = entries[:LEADERBOARD_LIMIT]
    rows = [_rich_board_row(entry, self_display_name=self_display_name) for entry in top_entries]

    extra_rows, trailing = _rich_self_appendage(
        entries=entries,
        visible_count=len(top_entries),
        self_display_name=self_display_name,
        self_resolved_total=self_resolved_total,
    )
    rows.extend(extra_rows)

    body = rich.table(
        ["#", "Caller", "PnL", "Hit", "Calls"],
        rows,
        # Center the rank column so medal emoji (🥇/🥈/🥉) and plain rank
        # numbers (4, 5, …) sit consistently instead of the medals
        # hugging the left edge.
        ["center", "left", "right", "right", "right"],
    )

    header_suffix = _FILTER_HEADER_SUFFIX.get(active_filter, "")
    blocks = [f"# Leaderboard{header_suffix}", "", body, ""]
    if trailing is not None:
        blocks.extend([trailing, ""])
    blocks.append(_footer_ranking_rich(payload))
    # Blank line before the CTA: Telegram soft-joins single-newline
    # lines into one paragraph, which would run the italic footer and
    # the CTA together on one line.
    blocks.append("")
    blocks.append("/alpha to add a call, /me for your own.")
    return "\n".join(blocks)


def _rich_board_row(
    entry: dict[str, Any],
    *,
    self_display_name: str | None,
) -> list[str]:
    """One leaderboard table row ``[#, caller, pnl, hit, calls]``.

    The caller's own name is wrapped in ``**bold**`` so they can find
    themselves at a glance (the rich-table equivalent of the plain
    renderer's ``<b>``); the name is escaped via
    :func:`merlin_bot.rich.escape_cell` first so a display name with
    Markdown/HTML/pipe characters can't break the table or inject
    formatting. Podium ranks keep their medal emoji; below the podium
    the ``#`` column carries the bare rank number.
    """
    rank = int(entry.get("rank") or 0)
    raw_name = str(entry.get("display_name") or "anonymous")
    name = rich.escape_cell(raw_name)
    if self_display_name is not None and raw_name == self_display_name:
        name = f"**{name}**"
    prefix = _PODIUM_EMOJI.get(rank, str(rank))
    return [
        prefix,
        name,
        _board_pnl_cell(entry.get("weighted_pnl_pct")),
        _board_hit_cell(entry.get("accuracy_pct")),
        _board_calls_cell(entry.get("calls_resolved")),
    ]


def _rich_self_appendage(
    *,
    entries: list[dict[str, Any]],
    visible_count: int,
    self_display_name: str | None,
    self_resolved_total: int,
) -> tuple[list[list[str]], str | None]:
    """Rich-table analogue of :func:`_self_appendage_lines`.

    Returns ``(extra_rows, trailing_block)``:

    * ``extra_rows`` — table rows appended after the visible top-N: a
      ``…`` gap row then the caller's own row, when they rank outside
      the visible window. Empty otherwise.
    * ``trailing_block`` — the cold-start inspiring one-liner placed
      below the table, or ``None``.

    Same four behaviour branches and the same "anomalous missing
    caller" warning as the plain renderer.
    """
    if self_display_name is None:
        return [], None

    caller_row = _find_caller_row(entries, self_display_name)
    if caller_row is not None:
        caller_rank = _safe_int(caller_row.get("rank"))
        if caller_rank <= visible_count:
            return [], None
        gap_row = [_GAP_LINE, "", "", "", ""]
        return [gap_row, _rich_board_row(caller_row, self_display_name=self_display_name)], None

    if self_resolved_total == 0:
        return [], _cold_start_message()

    _log.warning(
        "caller %r has %d resolved calls but is missing from /board",
        self_display_name,
        self_resolved_total,
    )
    return [], None


def _footer_ranking_rich(payload: dict[str, Any]) -> str:
    """Italic footer for the rich board: ranking explainer + activity.

    Markdown-italic (``_..._``) twin of :func:`_footer_ranking_line`,
    with the identical zero-activity omission rule and pluralisation.
    """
    open_calls = _safe_int(payload.get("open_calls_total"))
    open_callers = _safe_int(payload.get("open_callers_total"))

    base = "Ranked by recent PnL, weighted by track record."
    if open_calls <= 0 or open_callers <= 0:
        return f"_{base}_"

    call_word = "call" if open_calls == 1 else "calls"
    user_word = "user" if open_callers == 1 else "users"
    verb = "is" if open_calls == 1 else "are"
    return (
        f"_{base} {open_calls} open {call_word} by {open_callers} {user_word} {verb} in progress._"
    )


def _board_pnl_cell(pnl: object) -> str:
    """Signed two-decimal PnL for a board table cell: ``+2.50%``."""
    if pnl is None:
        return "—"
    try:
        amount = float(pnl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{amount:+.2f}%"


def _board_hit_cell(accuracy: object) -> str:
    """Integer hit-rate percent for a board table cell: ``80%``."""
    if accuracy is None:
        return "—"
    try:
        amount = float(accuracy)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{amount:.0f}%"


def _board_calls_cell(resolved: object) -> str:
    """Bare resolved-count for a board table cell: ``10``."""
    return str(_safe_int(resolved))


def _render_board(
    payload: dict[str, Any],
    *,
    self_display_name: str | None = None,
    self_resolved_total: int = 0,
    active_filter: str = "all",
) -> str:
    """Compose the ``/board`` reply text (HTML).

    Empty entries → the honest empty-state. Non-empty → medal-emoji
    top-3 + plain rank-number rest, with each row carrying:

    * Display name (caller's row wrapped in ``<b>...</b>``).
    * Conviction-weighted PnL %, signed, two decimals — the primary
      headline per the locked ``primary_metric = pnl_primary`` choice.
    * Hit-rate %, no decimals — the secondary number.
    * Resolved-count ("8 calls" / "1 call").

    After the visible top-N (``LEADERBOARD_LIMIT``), one of three
    appendages may follow (see ``_self_appendage_lines``):

    * "…" + caller's row, when they rank outside the top-N.
    * Inspiring one-liner, when they have zero resolved calls.
    * Nothing, when they're already inside the top-N (their row is
      bolded inline) or /me failed.

    A footer demystifies the ranking method in one line so users
    don't wonder why a +10% one-shot ranks mid-pack.

    ``active_filter`` controls the header-line suffix ("Leaderboard
    · Day Traders (≤24h)" / "· Swing Traders (1d-3wk)" /
    "· Position Traders (>3wk)") and is also passed through to the
    empty-state branch so users see WHICH slice is empty rather than
    wondering whether the whole product has no data.
    """
    entries = payload.get("entries") or []
    if not entries:
        return _empty_state_text(active_filter=active_filter)

    top_entries = entries[:LEADERBOARD_LIMIT]

    header_suffix = _FILTER_HEADER_SUFFIX.get(active_filter, "")
    lines = [f"<b>Leaderboard</b>{header_suffix}"]
    for entry in top_entries:
        lines.append(_render_row(entry, self_display_name=self_display_name))

    lines.extend(
        _self_appendage_lines(
            entries=entries,
            visible_count=len(top_entries),
            self_display_name=self_display_name,
            self_resolved_total=self_resolved_total,
        ),
    )

    lines.append("")
    lines.append(_footer_ranking_line(payload))
    lines.append("/alpha to add a call, /me for your own.")
    return "\n".join(lines)


def _footer_ranking_line(payload: dict[str, Any]) -> str:
    """Italic footer: ranking explainer + platform activity heartbeat.

    Two sentences in one italic line:

    1. The ranking explainer ("Ranked by recent PnL, weighted by
       track record.") — unchanged from the original copy.
    2. Platform activity ("X open calls by Y users are in progress.")
       — pulled from ``open_calls_total`` + ``open_callers_total`` on
       the wire payload. Pluralised independently: ``1 call`` /
       ``2 calls``, ``1 user`` / ``2 users``, ``1 call is`` /
       ``2 calls are``.

    The activity sentence is OMITTED when ``open_calls_total == 0``:
    "0 open calls by 0 users are in progress" reads as a downtime
    signal even though the leaderboard above may be populated, and
    on a green-field deploy it would inadvertently leak that the
    platform is empty. Cold-start should look quiet, not broken.

    Defensive int-coercion: ``BoardResponse`` defaults both counts
    to ``0`` on the contract, but a mid-deploy stale schema or a
    non-numeric value (``None`` / str) shouldn't crash /board.
    """
    open_calls = _safe_int(payload.get("open_calls_total"))
    open_callers = _safe_int(payload.get("open_callers_total"))

    base = "<i>Ranked by recent PnL, weighted by track record."
    if open_calls <= 0 or open_callers <= 0:
        return base + "</i>"

    call_word = "call" if open_calls == 1 else "calls"
    user_word = "user" if open_callers == 1 else "users"
    verb = "is" if open_calls == 1 else "are"
    return (
        f"{base} "
        f"{open_calls} open {call_word} by {open_callers} {user_word} "
        f"{verb} in progress.</i>"
    )


def _self_appendage_lines(
    *,
    entries: list[dict[str, Any]],
    visible_count: int,
    self_display_name: str | None,
    self_resolved_total: int,
) -> list[str]:
    r"""Build the "…\n{caller's row}" or inspiring-message appendage.

    Three behaviour branches, in priority order:

    1. **/me failed** (``self_display_name is None``): no appendage.
       We don't know who the caller is, so we can't show their row or
       a meaningful nudge.
    2. **Caller in the visible top-N**: no appendage. Their row is
       already bolded above; a second copy below would be redundant.
    3. **Caller outside the visible top-N**, has resolved calls
       (rank exists in ``entries``): show ``"…"`` then their row.
       Lets a rank-42 caller see their position without scrolling
       past 30+ podium rows of unrelated people.
    4. **Caller has zero resolved calls** (``self_resolved_total ==
       0``): inspiring one-liner instead of a row. They aren't on
       the leaderboard at all — the API filters cold-start sources
       — but we tell them what unlocks a spot.

    The "has resolved calls but isn't in ``entries``" combination is
    treated as anomalous (mid-recompute snapshot lag, schema drift)
    and silenced — no appendage — rather than rendered with a
    placeholder row. Loud-fail in logs, not in the user's UI.
    """
    if self_display_name is None:
        return []

    caller_row = _find_caller_row(entries, self_display_name)

    if caller_row is not None:
        caller_rank = _safe_int(caller_row.get("rank"))
        if caller_rank <= visible_count:
            return []
        return [
            _GAP_LINE,
            _render_row(caller_row, self_display_name=self_display_name),
        ]

    if self_resolved_total == 0:
        return ["", _cold_start_message()]

    # Has resolved calls but not in the leaderboard — anomalous, see
    # the docstring above. Log so we can investigate without scaring
    # the caller.
    _log.warning(
        "caller %r has %d resolved calls but is missing from /board",
        self_display_name,
        self_resolved_total,
    )
    return []


def _find_caller_row(
    entries: list[dict[str, Any]],
    self_display_name: str,
) -> dict[str, Any] | None:
    """Locate the caller's row by display_name.

    Display names are user-chosen and not strictly unique, but
    ``Source.display_name`` is capped at 40 chars and collisions are
    rare in practice. A two-Yuri collision would highlight + append
    the first row found, which is mildly wrong but never wrong-data
    — both Yuris would see one of their PnL on the board. Acceptable
    until we surface a stable, non-PII caller-rank field on /me.
    """
    for entry in entries:
        if entry.get("display_name") == self_display_name:
            return entry
    return None


def _safe_int(value: object) -> int:
    """``int(value)`` with a 0 fallback for None / non-numeric.

    Wire row fields land here as ``object`` after ``dict.get`` —
    mypy strict mode rejects a direct ``int(value)`` so we narrow via
    isinstance for ints/floats/strs (the realistic JSON-shape inputs)
    and fall through to 0 for anything else.
    """
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _cold_start_message() -> str:
    """Inspiring one-liner for a caller with zero resolved calls.

    Frames the gap as a milestone they can reach ("lands you on the
    board") rather than a missing feature. No version markers, no
    "soon" / "coming soon", no scoring jargon. The call to action
    (/alpha) is implicit via the footer that follows.
    """
    return "Your first resolution lands you on the board."


def _render_row(
    entry: dict[str, Any],
    *,
    self_display_name: str | None,
) -> str:
    """One leaderboard row in HTML, with optional self-bolding."""
    rank = int(entry.get("rank") or 0)
    raw_name = str(entry.get("display_name") or "anonymous")
    pnl = entry.get("weighted_pnl_pct")
    accuracy = entry.get("accuracy_pct")
    resolved = int(entry.get("calls_resolved") or 0)

    name_escaped = html.escape(raw_name)
    if self_display_name is not None and raw_name == self_display_name:
        # Wrap the name in <b> so the caller can find themselves
        # without scanning the whole list. We bold only the name (not
        # the whole row) so the layout stays uniform.
        name_rendered = f"<b>{name_escaped}</b>"
    else:
        name_rendered = name_escaped

    prefix = _PODIUM_EMOJI.get(rank, f"{rank}.")
    pnl_chunk = _format_pnl_chunk(pnl)
    accuracy_chunk = _format_accuracy_chunk(accuracy)
    calls_chunk = _format_calls_chunk(resolved)

    return f"{prefix} {name_rendered} — {pnl_chunk} · {accuracy_chunk} · {calls_chunk}"


def _format_pnl_chunk(pnl: object) -> str:
    """Signed PnL with two decimals: ``+2.50% PnL`` / ``-1.30% PnL``.

    ``None`` only happens defensively — the API filters cold-start
    sources OUT of the leaderboard already — but we render an em-dash
    rather than crash on a future schema change that lets a NULL
    through. Non-numeric values fall back to the em-dash too so the
    handler never raises on a malformed wire row.
    """
    if pnl is None:
        return "— PnL"
    try:
        amount = float(pnl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— PnL"
    return f"{amount:+.2f}% PnL"


def _format_accuracy_chunk(accuracy: object) -> str:
    """Hit-rate with no decimals: ``50% hit``. Em-dash for None."""
    if accuracy is None:
        return "— hit"
    try:
        amount = float(accuracy)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "— hit"
    return f"{amount:.0f}% hit"


def _format_calls_chunk(resolved: int) -> str:
    """Pluralised resolved-count: ``1 call`` / ``8 calls``."""
    suffix = "" if resolved == 1 else "s"
    return f"{resolved} call{suffix}"


# ---------------------------------------------------------------------------
# Bot-voice strings
# ---------------------------------------------------------------------------


def _empty_state_text(*, active_filter: str = "all") -> str:
    """Empty-state copy for when the requested slice has no resolutions.

    Frames the gap as a product choice ("unlocks after the first
    resolutions") rather than a missing feature. Avoids version
    markers, internal scoring nouns, and any "soon" / "coming soon"
    language that ages badly.

    Filter-aware: a populated "All" board paired with an empty
    ``day`` / ``swing`` / ``position`` slice is the realistic state
    on day one (and the whole reason the ``/board`` redesign got
    buttons), so the empty-state for a filtered view tells the user
    WHICH slice is empty and nudges them to the *other three*
    filters by name. ``position`` gets a slightly longer copy because
    at v0 it's the expected day-one state (zero >3wk resolutions
    until the first ones mature) — the extra sentence explains the
    structural reason so users don't read it as a missing feature.
    """
    if active_filter == "day":
        return (
            "No Day Trader resolutions yet (≤24h horizon).\n"
            "\n"
            "Tap Swing Traders, Position Traders, or All to see the populated boards."
        )
    if active_filter == "swing":
        return (
            "No Swing Trader resolutions yet (1d-3wk horizon).\n"
            "\n"
            "Tap Day Traders, Position Traders, or All to see the populated boards."
        )
    if active_filter == "position":
        return (
            "No Position Trader resolutions yet (>3wk horizon).\n"
            "\n"
            "These take 3+ weeks to mature — "
            "tap Day Traders, Swing Traders, or All for the active boards."
        )
    return (
        "Leaderboard unlocks after the first resolutions.\n"
        "\n"
        "/alpha to add a call, or /me for your own open ones."
    )


def _temporary_error_text() -> str:
    """API down / unexpected error — recoverable, just try again."""
    return "Something's off on our end. Try /board again in a moment."
