"""Outbound notification templates.

Every string follows the bot-voice rules (ARCHITECTURE §16 invariant 16):

- No version markers (``v0``, ``MVP``, ``Sprint``).
- No internal model nouns (``claim``, ``hypothesis``, ``regret``,
  ``source``).
- No raw enum literals (``pending``, ``resolved``).
- No ISO timestamps in user-visible text. Calendar dates only.
- No labels-with-colons. Inline prose only.
- No "$5 simulated size" or any conviction-to-dollar mapping (Q-028).
- No limitation hedges ("not scored at v0", "kept for the record") (Q-028).

Pure functions — no I/O, no globals. The notifier consumes the
finished string and POSTs it as-is.
"""

from __future__ import annotations

from merlin_core.application.use_cases.board_diff import BoardChangeScenario
from merlin_core.contracts.horizons import humanise_horizon
from merlin_core.domain import Direction
from merlin_core.domain.scoring import FLAT_THRESHOLD_PCT, is_correct_call, is_flat

# Re-exported for callers that historically reached for it from this
# module (telegram templates were the original home of the flat-cutoff
# constant). The single definition now lives in ``merlin_core.domain``
# so the resolver's hit-rate computation and these templates can't
# disagree on what "won" means.
__all__ = [
    "FLAT_THRESHOLD_PCT",
    "board_climbed_top3",
    "board_dropped_in_top3",
    "board_entered_top10",
    "board_fell_out_top10",
    "board_lost_throne",
    "board_took_throne",
    "first_call_nudge",
    "invitee_joined",
    "outcome_flat",
    "outcome_lost",
    "outcome_won",
    "rejected_asset_mutation",
    "render_board_change",
    "render_outcome",
    "resolution_failed_final",
]


def _direction_phrase(direction: Direction) -> str:
    """Render direction for past-tense outcome notifications."""
    return "Long" if direction == "long" else "Short"


#: Tail appended to every won / lost / flat notification — the recompute
#: that just fired changed (or pinned) the user's leaderboard standing, so
#: surface where they can see the new state. /me and /board are bot
#: commands and Telegram auto-links them; we DO NOT MarkdownV2-escape the
#: slash here because the notifier sends in HTML mode (Defaults.parse_mode
#: differs between sender paths, but neither escapes ``/`` — the literal
#: form below is safe in both).
_BOARD_ME_CTA: str = "Tap /me or /board for the rest."

#: Paragraph break between the resolution body and the PnL + CTA tail.
#: Telegram renders a single ``\n`` as a soft line wrap and ``\n\n`` as
#: a paragraph gap. The user-facing message reads as two visual blocks:
#: "what just happened to your call" first, "your new standing + how to
#: see it" second. Pinned as a constant so a test can assert the
#: paragraph break landed and a future template tweak can't silently
#: glue the two paragraphs back together.
_TAIL_SEPARATOR: str = "\n\n"


def _pnl_and_cta(weighted_pnl_pct: float | None) -> str:
    """Render the trailing PnL chunk + /me · /board CTA as one paragraph.

    Two shapes:

    * ``weighted_pnl_pct`` is a number → ``"Your PnL is now +X.X%. Tap
      /me or /board for the rest."``
    * ``weighted_pnl_pct`` is ``None`` (recompute failed, cold-start
      edge) → just the CTA. We never render ``"Your PnL is now None"``,
      and we DO NOT drop the CTA — readers always want a path back to
      their standing after a resolution.

    The returned string is the tail block that gets concatenated onto
    the resolution body via ``_TAIL_SEPARATOR`` — see the
    won/lost/flat templates below.
    """
    if weighted_pnl_pct is None:
        return _BOARD_ME_CTA
    return f"Your PnL is now {weighted_pnl_pct:+.1f}%. {_BOARD_ME_CTA}"


def outcome_won(
    *,
    asset_symbol: str,
    direction: Direction,
    horizon_seconds: int,
    pct: float,
    weighted_pnl_pct: float | None = None,
) -> str:
    """Notification for a correct call (sign matches direction, |pct| ≥ flat threshold).

    ``weighted_pnl_pct`` is the source's NEW PnL post-recompute, threaded
    in by the resolver from the inline call to
    ``recompute_source_reputation._recompute_async``. ``None`` falls
    back to the original wording (no PnL chunk) but the /me·/board
    CTA still renders — readers always want a path back to their
    standing after a resolution.
    """
    return (
        f"🎉 Your {asset_symbol} call resolved. "
        f"{_direction_phrase(direction)} over {humanise_horizon(horizon_seconds)} "
        f"ended {pct:+.1f}% — you called it right!"
        f"{_TAIL_SEPARATOR}{_pnl_and_cta(weighted_pnl_pct)}"
    )


def outcome_lost(
    *,
    asset_symbol: str,
    direction: Direction,
    horizon_seconds: int,
    pct: float,
    weighted_pnl_pct: float | None = None,
) -> str:
    """Notification for an incorrect call (sign mismatch, |pct| ≥ flat threshold).

    Same ``weighted_pnl_pct`` contract as :func:`outcome_won`. Even a
    losing call updates the leaderboard PnL (could pull it down, could
    leave it positive if other calls compensate), so the CTA + PnL
    chunk surface the new standing — no different from the won path.
    """
    return (
        f"🙂‍↔️ Your {asset_symbol} call resolved. "
        f"{_direction_phrase(direction)} over {humanise_horizon(horizon_seconds)} "
        f"ended {pct:+.1f}% — the market went the other way."
        f"{_TAIL_SEPARATOR}{_pnl_and_cta(weighted_pnl_pct)}"
    )


def outcome_flat(
    *,
    asset_symbol: str,
    pct: float,
    weighted_pnl_pct: float | None = None,
) -> str:
    """Notification for a wash outcome (|pct| < flat threshold).

    Flats DO contribute to PnL (``flat_handling=honest_signed`` per
    the design conversation): a +0.3% flat win adds +0.3% x 1 to the
    source's PnL even though hit rate counts it as not-correct
    (wash_zero). So the CTA + PnL chunk render on flat outcomes too.
    """
    return (
        f"📝 Your {asset_symbol} call resolved flat at {pct:+.2f}%. "
        f"Counts as a wash."
        f"{_TAIL_SEPARATOR}{_pnl_and_cta(weighted_pnl_pct)}"
    )


def rejected_asset_mutation(*, asset_symbol: str) -> str:
    """Notification when CoinGecko 404s an asset between t0 and t_resolve (Q-023)."""
    return (
        f"Your {asset_symbol} call couldn't be settled — "
        f"the token's listing changed during your window. "
        f"No effect on your record."
    )


def resolution_failed_final(*, asset_symbol: str) -> str:
    """Notification after the final RQ retry — operator now owns the rerun."""
    return (
        f"We're still trying to settle your {asset_symbol} call — "
        f"market data is taking longer than usual. We'll keep at it."
    )


def first_call_nudge() -> str:
    """Activation DM for an onboarded, named user who hasn't bet yet (Q-031).

    Sent by the worker's first-call nudge cron to testers who signed up
    and picked a name but have zero calls. Plain text — the notifier
    sends with no ``parse_mode``, so ``/board`` auto-links and needs no
    MarkdownV2 escaping (same as the board-change templates below).

    The example bet doubles as the instruction: any plain-text reply
    from an idle user is piped straight through the bot's freeform
    extractor to the preview (the ``/alpha`` ConversationHandler's
    text entry point), so "just reply with your bet" works verbatim.
    "call" / "bet" are the public nouns; no internal model terms, no
    version markers, no label-colons (MVP §15 invariant 16).
    """
    return (
        "📈 You're in — time to make your first call! "
        "Just reply with your bet in plain English, for example:"
        "\n\n"
        '"BTC long 5 days, recovery after the geopolitical news"'
        "\n\n"
        "I'll lock it in and track how it plays out. "
        "Curious how everyone else is doing? Check /board."
    )


def invitee_joined(*, name: str) -> str:
    """Notification to the inviter when someone they invited finishes onboarding.

    Fires exactly once per invitee, on the transition from "no display
    name" to "first display name set" inside ``PATCH /me``. The string
    addresses the inviter in second person ("your invite code") so the
    reader knows this is about their own outstanding code.

    ``name`` is the invitee's freshly-chosen display name, already
    trimmed to the 40-char cap by ``PATCH /me``'s validation. The
    template does not re-escape — the notifier sends in plain text.
    """
    return f"🤝🏻 {name} has joined using your invite code!"


def render_outcome(
    *,
    asset_symbol: str,
    direction: Direction,
    horizon_seconds: int,
    pct: float,
    weighted_pnl_pct: float | None = None,
) -> str:
    """Dispatch to the right outcome template by sign and magnitude.

    Single entry point for the resolver. Defers the won/lost decision
    to ``merlin_core.domain.scoring.is_correct_call`` so this view and
    the reputation recompute view agree forever.

    ``weighted_pnl_pct`` is the freshly-recomputed PnL for the source,
    propagated to whichever template fires. ``None`` (resolver's inline
    recompute failed / source still cold-start) keeps the original
    wording for the body but still appends the /me·/board CTA so the
    user always has a route back to their standing.
    """
    if is_flat(pct):
        return outcome_flat(
            asset_symbol=asset_symbol,
            pct=pct,
            weighted_pnl_pct=weighted_pnl_pct,
        )
    if is_correct_call(direction, pct):
        return outcome_won(
            asset_symbol=asset_symbol,
            direction=direction,
            horizon_seconds=horizon_seconds,
            pct=pct,
            weighted_pnl_pct=weighted_pnl_pct,
        )
    return outcome_lost(
        asset_symbol=asset_symbol,
        direction=direction,
        horizon_seconds=horizon_seconds,
        pct=pct,
        weighted_pnl_pct=weighted_pnl_pct,
    )


# ---------------------------------------------------------------------------
# Board-change notifications (rank movements on /board).
#
# Proactive DMs the worker sends when a resolved call moves a source
# across a band worth caring about (top-10 boundary, the throne, or a
# shuffle inside the top 3). Plain text with a leading emoji — sent by
# the same ``TelegramNotifier`` (no ``parse_mode``), so no MarkdownV2
# escaping; ``#``, ``->`` and ``/command`` render literally and Telegram
# auto-links the command. Each ends with a /board or /alpha hook to pull
# the reader back into the bot. "call" is the public noun (never
# "claim"/"source"); no label-colons; no ISO timestamps.
# ---------------------------------------------------------------------------


def board_entered_top10(*, new_rank: int) -> str:
    """Source crossed into the top 10 (gated to a >=20-source board)."""
    return f"🚀 You cracked the top 10! Welcome to #{new_rank} — the air's thinner up here. /board"


def board_fell_out_top10(*, old_rank: int) -> str:
    """Source dropped out of the top 10 (gated to a >=20-source board)."""
    return (
        f"📉 Ouch — you just dropped out of the top 10 (you were #{old_rank}). "
        f"Don't let it slide. One sharp call claws it back. /alpha"
    )


def board_took_throne() -> str:
    """Source took the #1 spot."""
    return "👑 You did it — you're #1. Everyone's chasing you now. Stay ruthless. /board"


def board_lost_throne(*, new_rank: int) -> str:
    """Source lost the #1 spot."""
    return f"🥊 Dethroned. Someone knocked you down to #{new_rank}. Time to take it back. /alpha"


def board_climbed_top3(*, old_rank: int, new_rank: int) -> str:
    """Source climbed in a move that touches the top 3."""
    return (
        f"🔥 On a tear — you climbed from #{old_rank} to #{new_rank}. The podium's in reach. /board"
    )


def board_dropped_in_top3(*, old_rank: int, new_rank: int) -> str:
    """Source slipped in a move that touches the top 3.

    Also covers dropping out of the top 3 (e.g. #3 -> #4) — the wording
    reads correctly for both an in-podium slip and a fall just off it.
    """
    return (
        f"⚠️ Slipping — you fell from #{old_rank} to #{new_rank}. "
        f"The top 3 is brutal. Defend your spot. /board"
    )


def _require_rank(rank: int | None, *, scenario: BoardChangeScenario, which: str) -> int:
    """Unwrap a rank the scenario guarantees is present.

    The classifier only emits a scenario when the rank(s) it renders are
    on-board, so ``None`` here means a programming error. Raise loudly
    (the hook wraps this best-effort) rather than render ``#None``.
    """
    if rank is None:
        msg = f"{scenario} requires a non-None {which}_rank"
        raise ValueError(msg)
    return rank


def render_board_change(
    scenario: BoardChangeScenario,
    *,
    old_rank: int | None,
    new_rank: int | None,
) -> str:
    """Dispatch a ``BoardChangeScenario`` to its template.

    Single entry point for the worker's board-change hook (mirrors
    ``render_outcome``). Each scenario only reads the rank(s) the
    classifier guarantees are present.
    """
    if scenario is BoardChangeScenario.ENTERED_TOP10:
        return board_entered_top10(
            new_rank=_require_rank(new_rank, scenario=scenario, which="new"),
        )
    if scenario is BoardChangeScenario.FELL_OUT_TOP10:
        return board_fell_out_top10(
            old_rank=_require_rank(old_rank, scenario=scenario, which="old"),
        )
    if scenario is BoardChangeScenario.TOOK_THRONE:
        return board_took_throne()
    if scenario is BoardChangeScenario.LOST_THRONE:
        return board_lost_throne(
            new_rank=_require_rank(new_rank, scenario=scenario, which="new"),
        )
    if scenario is BoardChangeScenario.CLIMBED_TOP3:
        return board_climbed_top3(
            old_rank=_require_rank(old_rank, scenario=scenario, which="old"),
            new_rank=_require_rank(new_rank, scenario=scenario, which="new"),
        )
    return board_dropped_in_top3(
        old_rank=_require_rank(old_rank, scenario=scenario, which="old"),
        new_rank=_require_rank(new_rank, scenario=scenario, which="new"),
    )
