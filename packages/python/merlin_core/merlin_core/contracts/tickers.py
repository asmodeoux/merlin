"""``GET /me/tickers`` — per-ticker breakdown of the user's resolved calls.

User-facing only, mirroring the ``BoardEntry`` shape: ticker, PnL %,
hit rate %, resolved-call count. The internal Bayesian-shrunk sort
score that drives the row order is NOT carried on the wire — same
discipline as ``BoardEntry`` (rank is rendered; ``sort_score`` is not).

Cold-start tickers (zero resolved calls) are filtered at the
repository boundary, so every ``TickerEntry`` row carries honest
non-Optional ``accuracy_pct`` / ``weighted_pnl_pct`` values.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TickerEntry(BaseModel):
    """One ticker's slice of the user's resolved-call portfolio.

    Drops the source-level ``MeResponse`` Optional dance — by
    construction, every row here represents at least one resolved
    call, so the PnL and accuracy are always honest numbers.

    ``calls_resolved`` is the count that drove the row's shrinkage,
    rendered as the trailing chunk ("2 calls") in the bot.
    """

    asset_symbol: str
    calls_resolved: int
    calls_correct: int
    accuracy_pct: float
    weighted_pnl_pct: float


class TickersResponse(BaseModel):
    """``GET /me/tickers`` response payload.

    ``entries`` is sorted server-side by Bayesian-shrunk PnL (the
    same ranking key as ``/board``) with ``calls_resolved`` /
    ``weighted_pnl_pct`` / ``asset_symbol`` tie-breakers. The bot
    renders the list in receive order — no client-side sort.

    An empty ``entries`` list signals "cold-start source" (no
    resolved calls yet); the bot renders an honest empty-state copy
    instead of a misleading zero row.
    """

    entries: list[TickerEntry] = Field(default_factory=list)
