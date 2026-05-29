"""``SubmitClaim`` use case — atomic claim commit + scheduled resolution.

Pure orchestration. Imports only from ``merlin_core.domain`` and
``merlin_core.application.ports`` — never from ``merlin_infrastructure``,
``merlin_api``, ``merlin_bot``, or any third-party adapter.

Per the bot-flow plan §2 "Bet capture":

1. Confirm the asset exists upstream (defence vs. forged ids).
2. INSERT-or-fetch the local ``assets`` row by ``coingecko_id``.
3. INSERT the ``hypotheses`` row (1:1 with the claim at v0, per Q-007).
4. INSERT the ``claims`` row (with ``parsed_via``, ``extracted_fields``,
   ``parse_metadata`` columns).
5. INSERT the ``events`` row with ``t_resolve = now() + horizon_seconds(horizon)``.
6. INSERT the ``attribution_ledger`` row (originator passthrough).
7. ``queue.enqueue_at(t_resolve, "merlin_worker.jobs.resolve_outcome",
   claim_id, job_id=f"resolve-{claim_id}")``.

All seven steps happen inside one DB transaction so a crash between
INSERTs leaves no partial state. The RQ enqueue runs after the
transaction commits (post-commit callback) so we never enqueue a job
for a claim that didn't land.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from merlin_core.application.ports import (
    AssetNotFoundError,
    JobQueuePort,
    MarketDataError,
    MarketDataPort,
)
from merlin_core.domain import Conviction, Direction, ExtractedFields, Horizon, ParseMetadata
from merlin_core.domain.horizons import HORIZON_SECONDS

if TYPE_CHECKING:
    pass


class SubmitClaimError(Exception):
    """Raised when ``SubmitClaim`` can't commit the claim.

    Concrete subclasses distinguish recoverable (re-prompt the user)
    from permanent (give up) failures.
    """


class UnknownAssetError(SubmitClaimError):
    """The supplied ``coingecko_id`` isn't recognised by CoinGecko."""


class MarketDataUnavailableError(SubmitClaimError):
    """CoinGecko was unreachable; the bot retries / falls back."""


@dataclass(frozen=True, slots=True)
class SubmitClaimInput:
    """Everything ``SubmitClaim`` needs to commit one claim."""

    source_id: UUID
    coingecko_id: str
    direction: Direction
    horizon: Horizon
    conviction: Conviction
    thesis_text: str
    extracted_fields: ExtractedFields
    parse_metadata: ParseMetadata
    submitted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubmitClaimResult:
    """What ``SubmitClaim`` returns on success."""

    claim_id: UUID
    asset_id: UUID
    asset_symbol: str
    submitted_at: datetime
    resolves_at: datetime
    queue_job_id: str


@runtime_checkable
class ClaimWriterPort(Protocol):
    """Persistence port for ``SubmitClaim``.

    The repository implementation lives in ``merlin_infrastructure``.
    Concrete adapter wraps SQLAlchemy + asyncpg + the append-only
    invariants. The use case sees a single ``write(...)`` operation
    that the adapter implements atomically.

    Returning the persisted ids back to the use case lets us compose
    the post-commit RQ enqueue cleanly — no leaking of DB primitives.
    """

    async def upsert_asset(
        self,
        *,
        coingecko_id: str,
        symbol: str,
        name: str,
    ) -> UUID:
        """INSERT-or-fetch an asset row keyed by ``coingecko_id``; return ``asset_id``."""
        ...

    async def write_claim(
        self,
        *,
        source_id: UUID,
        asset_id: UUID,
        direction: Direction,
        horizon: Horizon,
        conviction: Conviction,
        thesis_text: str,
        extracted_fields: ExtractedFields,
        parse_metadata: ParseMetadata,
        submitted_at: datetime,
        resolves_at: datetime,
    ) -> UUID:
        """Atomic 5-INSERT commit (claim + hypothesis + event + attribution + claim_event).

        Returns the new ``claim_id``. The repository is responsible
        for the transaction boundary and the append-only invariants.
        """
        ...


class SubmitClaim:
    """Orchestrate ``ClaimWriterPort`` + ``MarketDataPort`` + ``JobQueuePort``."""

    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        writer: ClaimWriterPort,
        queue: JobQueuePort,
    ) -> None:
        """Store ports; the use case is otherwise stateless."""
        self._market_data = market_data
        self._writer = writer
        self._queue = queue

    async def execute(self, payload: SubmitClaimInput) -> SubmitClaimResult:
        """Run the seven-step commit sequence."""
        # 1. Confirm the asset exists upstream.
        try:
            metadata = await self._market_data.get_asset_metadata(payload.coingecko_id)
        except AssetNotFoundError as e:
            raise UnknownAssetError(payload.coingecko_id) from e
        except MarketDataError as e:
            raise MarketDataUnavailableError(str(e)) from e

        # 2. INSERT-or-fetch the local ``assets`` row.
        asset_id = await self._writer.upsert_asset(
            coingecko_id=metadata.coingecko_id,
            symbol=metadata.symbol,
            name=metadata.name,
        )

        # 3-6. Atomic commit (hypothesis + claim + event + claim_event + attribution).
        submitted_at = payload.submitted_at or datetime.now(UTC)
        resolves_at = submitted_at + timedelta(seconds=HORIZON_SECONDS[payload.horizon])
        claim_id = await self._writer.write_claim(
            source_id=payload.source_id,
            asset_id=asset_id,
            direction=payload.direction,
            horizon=payload.horizon,
            conviction=payload.conviction,
            thesis_text=payload.thesis_text,
            extracted_fields=payload.extracted_fields,
            parse_metadata=payload.parse_metadata,
            submitted_at=submitted_at,
            resolves_at=resolves_at,
        )

        # 7. Schedule the resolver. Deterministic job_id (plan §3) lets
        #    operators cancel + rerun by natural key. RQ's
        #    ``validate_job_id`` rejects ``:`` (allows only
        #    ``[A-Za-z0-9_-]``); use a dash separator.
        job_id = self._queue.enqueue_at(
            resolves_at,
            "merlin_worker.jobs.resolve_outcome",
            str(claim_id),
            job_id=f"resolve-{claim_id}",
        )

        return SubmitClaimResult(
            claim_id=claim_id,
            asset_id=asset_id,
            asset_symbol=metadata.symbol,
            submitted_at=submitted_at,
            resolves_at=resolves_at,
            queue_job_id=job_id,
        )
