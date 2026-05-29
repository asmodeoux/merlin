"""Horizon constants and conversions.

MVP §15 invariant 4: "Horizons are exact seconds. ``24h = 86400 s``,
``30d = 2592000 s``, ``90d = 7776000 s``. No business-day math, no
calendar math, no DST."

Stdlib only.
"""

from __future__ import annotations

from datetime import timedelta

from merlin_core.domain.claim_draft import Horizon

# Exact seconds per the MVP §15 invariant 4 lock. Calendar/DST/business-day
# math is forbidden. These constants are the single source of truth.
HORIZON_SECONDS: dict[Horizon, int] = {
    "1h": 3_600,
    "4h": 14_400,
    "24h": 86_400,
    "3d": 259_200,
    "7d": 604_800,
    "14d": 1_209_600,
    "30d": 2_592_000,
    "90d": 7_776_000,
}

# User-facing labels for notification rendering. Bot voice (plan §6):
# never "14d" / "3d" / "24h" in user-visible text.
HORIZON_HUMAN: dict[Horizon, str] = {
    "1h": "1 hour",
    "4h": "4 hours",
    "24h": "24 hours",
    "3d": "3 days",
    "7d": "1 week",
    "14d": "2 weeks",
    "30d": "1 month",
    "90d": "3 months",
}


def horizon_to_timedelta(horizon: Horizon) -> timedelta:
    """Return the ``timedelta`` corresponding to a quantised horizon code."""
    return timedelta(seconds=HORIZON_SECONDS[horizon])


def horizon_human(horizon: Horizon) -> str:
    """Return the user-facing label for a horizon code.

    Used by notification templates and the preview message. Never call
    this from inside a database/contract layer — those use the raw
    horizon code.
    """
    return HORIZON_HUMAN[horizon]
