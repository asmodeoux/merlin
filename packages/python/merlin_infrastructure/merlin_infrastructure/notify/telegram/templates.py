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


def outcome_won(
    *, asset_symbol: str, direction: Direction, horizon_seconds: int, pct: float
) -> str:
    """Notification for a correct call (sign matches direction, |pct| ≥ flat threshold)."""
    return (
        f"🎉 Your {asset_symbol} call resolved. "
        f"{_direction_phrase(direction)} over {humanise_horizon(horizon_seconds)} "
        f"ended {pct:+.1f}% — you called it right!"
    )


def outcome_lost(
    *, asset_symbol: str, direction: Direction, horizon_seconds: int, pct: float
) -> str:
    """Notification for an incorrect call (sign mismatch, |pct| ≥ flat threshold)."""
    return (
        f"🙂‍↔️ Your {asset_symbol} call resolved. "
        f"{_direction_phrase(direction)} over {humanise_horizon(horizon_seconds)} "
        f"ended {pct:+.1f}% — the market went the other way."
    )


def outcome_flat(*, asset_symbol: str, pct: float) -> str:
    """Notification for a wash outcome (|pct| < flat threshold)."""
    return f"📝 Your {asset_symbol} call resolved flat at {pct:+.2f}%. Counts as a wash."


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


def render_outcome(
    *,
    asset_symbol: str,
    direction: Direction,
    horizon_seconds: int,
    pct: float,
) -> str:
    """Dispatch to the right outcome template by sign and magnitude.

    Single entry point for the resolver. Defers the won/lost decision
    to ``merlin_core.domain.scoring.is_correct_call`` so this view and
    the reputation recompute view agree forever.
    """
    if is_flat(pct):
        return outcome_flat(asset_symbol=asset_symbol, pct=pct)
    if is_correct_call(direction, pct):
        return outcome_won(
            asset_symbol=asset_symbol,
            direction=direction,
            horizon_seconds=horizon_seconds,
            pct=pct,
        )
    return outcome_lost(
        asset_symbol=asset_symbol,
        direction=direction,
        horizon_seconds=horizon_seconds,
        pct=pct,
    )
