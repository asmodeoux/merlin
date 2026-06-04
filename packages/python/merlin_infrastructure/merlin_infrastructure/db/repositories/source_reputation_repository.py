"""``SourceReputationRepository`` — append-only history snapshots.

Backs ``SourceReputationRepositoryPort``. One INSERT per
``recompute_source_reputation`` invocation; one ``ORDER BY effective_at
DESC LIMIT 1`` for the ``/me`` + ``/board`` readers.

MVP §15 invariant 10: no ``update()`` / ``delete()``. Postgres
triggers on the ``source_reputation_history`` table reject mutation
attempts at the DB layer too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from merlin_core.application.ports import (
    LeaderboardEntry,
    SourceReputationRecord,
    SourceReputationRepositoryPort,
)
from merlin_core.contracts.horizons import (
    HORIZON_DAY_TRADER_MAX_SECONDS,
    HORIZON_SWING_TRADER_MAX_SECONDS,
)
from merlin_core.domain.scoring import (
    accuracy_pct as compute_accuracy_pct,
)
from merlin_core.domain.scoring import (
    conviction_weight,
    directional_return_pct,
    shrunk_sort_score,
    weighted_mean_pnl_pct,
)
from sqlalchemy import and_, desc, func, select

from merlin_infrastructure.db.models import (
    Claim,
    Outcome,
    Source,
    SourceReputationHistory,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Four-mode horizon filter accepted by ``leaderboard``. Mirrors the
# wire-level ``HorizonFilter`` Literal in ``merlin_core.contracts.board``
# but kept as a plain frozenset so this adapter stays import-free of
# ``contracts.board``. The port doc-string promises rejection of
# anything outside this set.
_VALID_HORIZONS: frozenset[str] = frozenset({"all", "day", "swing", "position"})


@dataclass(slots=True)
class _SourceFold:
    """Mutable fold-bucket for the on-the-fly horizon slice.

    One bucket per ``source_id`` while we walk the joined
    ``outcomes`` ⋈ ``claims`` rows. The lists stay in lock-step
    (``directions[i]`` / ``convictions[i]`` / ``returns[i]`` belong
    to the same outcome) so the downstream scoring fold can pair
    them via ``zip(..., strict=True)``.
    """

    display_name: str | None
    directions: list[str] = field(default_factory=list)
    convictions: list[str] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)
    correct_count: int = 0
    last_call_at: datetime | None = None


def _to_record(row: SourceReputationHistory) -> SourceReputationRecord:
    """Project a SQLAlchemy row to the layer-crossing record."""
    return SourceReputationRecord(
        id=row.id,
        source_id=row.source_id,
        outcomes_resolved=row.outcomes_resolved,
        outcomes_correct=row.outcomes_correct,
        accuracy_pct=row.accuracy_pct,
        weighted_pnl_pct=row.weighted_pnl_pct,
        sort_score=row.sort_score,
        triggering_outcome_id=row.triggering_outcome_id,
        effective_at=row.effective_at,
    )


class SourceReputationRepository(SourceReputationRepositoryPort):
    """SQLAlchemy adapter for ``SourceReputationRepositoryPort``."""

    def __init__(self, session: AsyncSession) -> None:
        """Construct the repository bound to an open ``AsyncSession``."""
        self._session = session

    async def insert_snapshot(
        self,
        *,
        source_id: str,
        outcomes_resolved: int,
        outcomes_correct: int,
        accuracy_pct: float | None,
        weighted_pnl_pct: float | None,
        sort_score: float,
        triggering_outcome_id: UUID | None,
        effective_at: datetime,
    ) -> SourceReputationRecord:
        """INSERT one history row. Flushes; does not commit."""
        row = SourceReputationHistory(
            source_id=source_id,
            outcomes_resolved=outcomes_resolved,
            outcomes_correct=outcomes_correct,
            accuracy_pct=accuracy_pct,
            weighted_pnl_pct=weighted_pnl_pct,
            sort_score=sort_score,
            triggering_outcome_id=triggering_outcome_id,
            effective_at=effective_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def latest_for_source(
        self,
        source_id: str,
    ) -> SourceReputationRecord | None:
        """Most-recent snapshot for the source, or ``None`` cold-start."""
        result = await self._session.execute(
            select(SourceReputationHistory)
            .where(SourceReputationHistory.source_id == source_id)
            .order_by(SourceReputationHistory.effective_at.desc())
            .limit(1),
        )
        row = result.scalars().first()
        if row is None:
            return None
        return _to_record(row)

    async def leaderboard(
        self,
        window: str,
        horizon: str = "all",
    ) -> list[LeaderboardEntry]:
        """Top sources by latest snapshot's ``sort_score``.

        ``window`` is accepted today so the route stays a thin
        pass-through; we ignore it for v0 (no time-window slicing yet).

        ``horizon`` is the four-mode "Day Traders" / "Swing Traders"
        / "Position Traders" filter:

        * ``"all"`` (default) — reads the snapshot table byte-for-byte
          (the original v0 path). Zero behavior change vs. the
          pre-filter ``/board``.
        * ``"day"`` / ``"swing"`` / ``"position"`` — bypasses the
          snapshot and recomputes per source by joining ``outcomes``
          ⋈ ``claims`` ⋈ ``sources`` with a bucketed
          ``claims.horizon_seconds`` predicate (≤24h / 24h-3wk / >3wk
          against ``HORIZON_DAY_TRADER_MAX_SECONDS`` and
          ``HORIZON_SWING_TRADER_MAX_SECONDS``), then folding via the
          same scoring helpers the worker recompute uses
          (``accuracy_pct``, ``conviction_weight``,
          ``directional_return_pct``, ``weighted_mean_pnl_pct``,
          ``shrunk_sort_score``). The snapshot table has no horizon
          dimension at v0, so a per-request recompute is the cheapest
          honest answer — and at v0 traffic (~100 claims/day per the
          recompute job docstring) it's a single round-trip plus
          in-Python folds.

        Either path:

        * Cold-start / empty-slice sources are dropped — the snapshot
          path filters by ``accuracy_pct IS NOT NULL``; the filtered
          path drops sources whose horizon-sliced bet count is zero.
        * ORDER BY ``sort_score`` DESC is the locked ranking key
          (Bayesian-shrunk PnL). Tiebreaker is bet-count DESC, then
          PnL DESC — a track-record bias on exact ties.
        * The display-name fallback is ``"anonymous"`` so the bot
          never renders a blank row.

        Raises ``ValueError`` on an unrecognised ``horizon`` value so
        a bad caller fails loudly rather than silently returning the
        unfiltered list.
        """
        del window  # v0 reads all-time only — the column wasn't sliced

        if horizon not in _VALID_HORIZONS:
            raise ValueError(
                f"unknown horizon filter: {horizon!r}; expected one of {sorted(_VALID_HORIZONS)}",
            )

        if horizon == "all":
            return await self._leaderboard_from_snapshots()
        return await self._leaderboard_by_horizon(horizon)

    async def _leaderboard_from_snapshots(self) -> list[LeaderboardEntry]:
        """All-time leaderboard from ``source_reputation_history``.

        The original v0 path: latest snapshot per source via a
        correlated subquery, joined to ``sources`` for the display
        name. Cold-start sources are filtered by
        ``accuracy_pct IS NOT NULL`` — equivalently, the matching
        DB CHECK guarantees ``weighted_pnl_pct IS NOT NULL`` and
        ``outcomes_resolved > 0`` for the same row.
        """
        latest_effective = (
            select(
                SourceReputationHistory.source_id.label("sid"),
                func.max(SourceReputationHistory.effective_at).label("max_eff"),
            )
            .group_by(SourceReputationHistory.source_id)
            .subquery()
        )

        stmt = (
            select(
                SourceReputationHistory,
                Source.display_name,
            )
            .join(
                latest_effective,
                (SourceReputationHistory.source_id == latest_effective.c.sid)
                & (SourceReputationHistory.effective_at == latest_effective.c.max_eff),
            )
            .join(
                Source,
                Source.source_id == SourceReputationHistory.source_id,
            )
            .where(SourceReputationHistory.accuracy_pct.isnot(None))
            .order_by(
                desc(SourceReputationHistory.sort_score),
                desc(SourceReputationHistory.outcomes_resolved),
                desc(SourceReputationHistory.weighted_pnl_pct),
            )
        )

        result = await self._session.execute(stmt)
        rows = result.all()

        entries: list[LeaderboardEntry] = []
        for rank, (snapshot, display_name) in enumerate(rows, start=1):
            entries.append(
                LeaderboardEntry(
                    rank=rank,
                    display_name=display_name or "anonymous",
                    calls_resolved=snapshot.outcomes_resolved,
                    calls_correct=snapshot.outcomes_correct,
                    # ``accuracy_pct`` and ``weighted_pnl_pct`` are
                    # both NOT NULL here because of the WHERE clause +
                    # the DB CHECK constraints; the ``or 0.0`` is
                    # defensive against a future schema change that
                    # silently drops NaNs through the float type.
                    accuracy_pct=float(snapshot.accuracy_pct or 0.0),
                    weighted_pnl_pct=float(snapshot.weighted_pnl_pct or 0.0),
                    last_call_at=snapshot.effective_at,
                ),
            )
        return entries

    async def _leaderboard_by_horizon(
        self,
        horizon: str,
    ) -> list[LeaderboardEntry]:
        """On-the-fly leaderboard for ``horizon in {"day","swing","position"}``.

        One SELECT joins ``outcomes`` ⋈ ``claims`` ⋈ ``sources`` with
        a horizon-bucket predicate; the per-source fold (accuracy,
        weighted PnL, shrunk sort score) happens in Python via the
        shared ``merlin_core.domain.scoring`` helpers. Keeping the
        per-bet math in one place — the same helpers the worker
        recompute uses — ensures the filtered leaderboard and the
        unfiltered one agree on every metric except the horizon slice.

        Bucket boundaries are continuous and non-overlapping:

        * ``day``:  ``horizon_seconds <= HORIZON_DAY_TRADER_MAX_SECONDS``
          (≤24h).
        * ``swing``: strictly greater than the day max, ≤
          ``HORIZON_SWING_TRADER_MAX_SECONDS`` (24h-3wk).
        * ``position``: strictly greater than the swing max (>3wk).

        Only ``status='resolved'`` outcomes count — Q-023
        ``rejected_asset_mutation`` rows and resolver-failure rows
        match the ``WHERE`` exclusion the recompute job applies.
        Sources with zero resolved outcomes in the slice are dropped
        (parity with the snapshot path's ``accuracy_pct IS NOT NULL``
        cold-start cutoff). At v0 the ``position`` bucket is expected
        to return empty until the first >3wk horizons mature; the bot
        renders a tailored empty-state for that case.

        The ``last_call_at`` projected on each entry is the most
        recent ``resolved_at`` for that source *within the
        horizon-sliced bets* — strictly more informative than the
        snapshot path's ``effective_at`` (which records when the
        snapshot was written, not when the last bet landed).
        """
        if horizon == "day":
            horizon_predicate = Claim.horizon_seconds <= HORIZON_DAY_TRADER_MAX_SECONDS
        elif horizon == "swing":
            horizon_predicate = and_(
                Claim.horizon_seconds > HORIZON_DAY_TRADER_MAX_SECONDS,
                Claim.horizon_seconds <= HORIZON_SWING_TRADER_MAX_SECONDS,
            )
        else:  # "position" — validated upstream in ``leaderboard``
            horizon_predicate = Claim.horizon_seconds > HORIZON_SWING_TRADER_MAX_SECONDS

        stmt = (
            select(
                Outcome.source_id,
                Source.display_name,
                Claim.direction,
                Claim.conviction,
                Outcome.observed_return_pct,
                Outcome.is_correct,
                Outcome.resolved_at,
            )
            .join(Claim, Claim.id == Outcome.claim_id)
            .join(Source, Source.source_id == Outcome.source_id)
            .where(
                Outcome.status == "resolved",
                horizon_predicate,
            )
        )

        result = await self._session.execute(stmt)
        rows = result.all()

        # Fold each joined outcome row into its source's bucket. The
        # CHECK constraints make ``observed_return_pct`` / ``is_correct``
        # non-NULL on resolved rows; the ``or 0.0`` and ``bool(...)``
        # casts mirror ``OutcomeRepository.fetch_resolved_for_reputation``
        # so a future schema relaxation can't silently feed a None into
        # the scoring pipeline.
        per_source: dict[str, _SourceFold] = {}
        for row in rows:
            sid = str(row.source_id)
            bucket = per_source.get(sid)
            if bucket is None:
                bucket = _SourceFold(display_name=row.display_name)
                per_source[sid] = bucket
            bucket.directions.append(str(row.direction))
            bucket.convictions.append(str(row.conviction))
            bucket.returns.append(float(row.observed_return_pct or 0.0))
            if bool(row.is_correct):
                bucket.correct_count += 1
            if bucket.last_call_at is None or row.resolved_at > bucket.last_call_at:
                bucket.last_call_at = row.resolved_at

        # Score each source with the same helpers the worker recompute
        # uses. Sort key triple mirrors the snapshot path:
        # ``sort_score`` DESC, ``resolved`` DESC, ``pnl_pct`` DESC.
        scored: list[tuple[float, int, float, LeaderboardEntry]] = []
        for bucket in per_source.values():
            resolved = len(bucket.returns)
            if resolved == 0:  # pragma: no cover
                # Structurally unreachable: a bucket only enters
                # ``per_source`` after the row-loop above appends at
                # least one ``return``. The guard documents the
                # invariant and protects against a future refactor
                # that decouples bucket creation from the append.
                continue
            accuracy = compute_accuracy_pct(bucket.correct_count, resolved)
            weights = [conviction_weight(c) for c in bucket.convictions]
            contributions = [
                directional_return_pct(d, r) * w
                for d, r, w in zip(
                    bucket.directions,
                    bucket.returns,
                    weights,
                    strict=True,
                )
            ]
            pnl_pct = weighted_mean_pnl_pct(contributions, weights)
            sort_score = shrunk_sort_score(pnl_pct, resolved)
            if accuracy is None or pnl_pct is None:  # pragma: no cover
                # Structurally unreachable: ``accuracy_pct`` returns
                # ``None`` only on zero-resolved (caught above);
                # ``weighted_mean_pnl_pct`` returns ``None`` only on
                # empty input or Σ(weights)==0 (impossible while
                # ``conviction_weight`` floors at 1). Skip rather
                # than crash if a future scoring tweak ever lets a
                # ``None`` slip through.
                continue
            entry = LeaderboardEntry(
                rank=0,  # filled after the sort
                display_name=bucket.display_name or "anonymous",
                calls_resolved=resolved,
                calls_correct=bucket.correct_count,
                accuracy_pct=float(accuracy),
                weighted_pnl_pct=float(pnl_pct),
                last_call_at=bucket.last_call_at,
            )
            scored.append((sort_score, resolved, float(pnl_pct), entry))

        scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [
            LeaderboardEntry(
                rank=rank,
                display_name=entry.display_name,
                calls_resolved=entry.calls_resolved,
                calls_correct=entry.calls_correct,
                accuracy_pct=entry.accuracy_pct,
                weighted_pnl_pct=entry.weighted_pnl_pct,
                last_call_at=entry.last_call_at,
            )
            for rank, (_, _, _, entry) in enumerate(scored, start=1)
        ]
