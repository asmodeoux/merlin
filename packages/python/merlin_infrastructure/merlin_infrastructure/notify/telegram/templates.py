"""Outbound notification templates.

Every string follows the bot-voice rules from the end-to-end plan §6:

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
    "invitee_joined",
    "outcome_flat",
    "outcome_lost",
    "outcome_won",
    "rejected_asset_mutation",
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
