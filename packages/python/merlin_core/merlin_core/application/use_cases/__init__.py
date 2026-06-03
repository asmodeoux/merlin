"""Use cases — orchestrate domain logic via ports.

Each use case takes its dependencies as Port parameters in its
constructor (no globals, no service-locator pattern) so unit tests can
pass in-memory fakes from `merlin_infrastructure` test fixtures.

Sprint 1 use cases:

- ``submit_claim.SubmitClaim`` — atomic claim commit + scheduled
  resolution (plan §2). Composes ``MarketDataPort`` +
  ``ClaimWriterPort`` + ``JobQueuePort``.
- ``resolve_outcome.ResolveOutcome`` (Sprint 1, worker side) —
  fetches t0 + t_resolve prices, writes outcome, enqueues reputation.
- ``recompute_reputation.RecomputeReputation`` (Sprint 1, worker side) —
  reactive per-outcome recompute.
"""

from merlin_core.application.use_cases.submit_claim import (
    ClaimWriterPort,
    MarketDataUnavailableError,
    SubmitClaim,
    SubmitClaimError,
    SubmitClaimInput,
    SubmitClaimResult,
    UnknownAssetError,
)
from merlin_core.application.use_cases.tickers import (
    TickerStats,
    compute_ticker_breakdown,
)

__all__ = [
    "ClaimWriterPort",
    "MarketDataUnavailableError",
    "SubmitClaim",
    "SubmitClaimError",
    "SubmitClaimInput",
    "SubmitClaimResult",
    "TickerStats",
    "UnknownAssetError",
    "compute_ticker_breakdown",
]
