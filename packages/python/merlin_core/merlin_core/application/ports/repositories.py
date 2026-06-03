"""Repository ports — Protocols for persistence-layer adapters.

Repository Protocols live in the **core** layer; concrete implementations
in ``merlin_infrastructure.db.repositories``. The application layer
depends only on these Protocols — adapters are wired at the edge by
``merlin_api.deps`` and ``merlin_worker.bootstrap``.

Protocols landed so far:

* ``SourceRepositoryPort`` — leaderboard identity + Admiralty grading.
* ``SourceIdentityRepositoryPort`` — PII boundary (raw Telegram IDs).
* ``InviteRepositoryPort`` — invite-only onboarding gate.
* ``ClaimRepositoryPort`` — minimum surface (``create`` +
  ``list_open_by_source`` + ``count_resolved_by_source`` +
  ``leaderboard``) for ``POST /alpha`` Confirm to persist a row and
  for ``GET /me`` + ``GET /board`` to be honest. The leaderboard query
  reads from ``source_reputation_history`` once outcomes start landing
  (Sprint 1).
* ``OutcomeRepositoryPort`` — append-only outcome events. The resolver
  INSERTs one row per ``resolve_outcome`` execution.
* ``SourceReputationRepositoryPort`` — append-only reputation
  snapshots written by the reactive ``recompute_source_reputation``
  job (Q-029, no daily cron).

Every append-only repository (MVP §15 invariants 7/8/10/12)
deliberately does NOT expose an ``update()`` / ``delete()`` method;
the database-layer triggers shipped with the matching migration are
the second line of defense.

All methods are async — every adapter is built on async SQLAlchemy.
``source_id`` is always the opaque ``'tg:<sha256>'`` string from
``merlin_core.domain.source_id.make_source_id``; raw Telegram IDs only
appear on ``SourceIdentityRepositoryPort``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

# ---------------------------------------------------------------------------
# Domain shapes used in repository signatures. Plain dataclasses so they're
# free of any ORM dependency — the application layer never sees SQLAlchemy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Read-side projection of a row in ``sources``.

    Frozen because consumers should never mutate a query result —
    persistence happens via repository methods, not by mutating the
    object you got back.
    """

    source_id: str
    display_name: str | None
    kind: str
    reliability: str
    credibility: int
    consent_version: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InviteRecord:
    """Read-side projection of a row in ``invites``."""

    id: UUID
    code: str
    created_by_source_id: str | None
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    redeemed_by_source_id: str | None
    revoked_at: datetime | None

    @property
    def is_redeemable(self) -> bool:
        """True iff this invite can still be consumed.

        A consumer should NOT trust this property in a race — the
        repository's redeem method does the actual conditional UPDATE
        with a ``WHERE redeemed_at IS NULL`` guard. This is convenient
        for logging and UI hints only.
        """
        return self.redeemed_at is None and self.revoked_at is None


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """Read-side projection of a row in ``claims``.

    Denormalized asset fields match the schema's denormalized columns;
    when the normalized ``assets`` table lands in a later sprint we'll
    add a richer projection alongside this one and leave this shape
    intact for the simple-listing paths.
    """

    id: UUID
    source_id: str
    asset_coingecko_id: str
    asset_symbol: str
    asset_name: str | None
    direction: str
    # Horizon as integer seconds. The continuous model replaces the
    # eight-bucket Literal so users can say "3 hours" without the
    # nearest-bucket UX trap. Validated at the API boundary to
    # ``[merlin_core.contracts.horizons.HORIZON_MIN_SECONDS,
    # HORIZON_MAX_SECONDS]`` (1h..90d).
    horizon_seconds: int
    conviction: str
    thesis_text: str
    asset_query: str | None
    submitted_at: datetime
    resolves_at: datetime
    parsed_via: str
    # Best-effort price-at-submit snapshot. ``None`` when CoinGecko
    # was unreachable at submit time or for rows that predate the
    # column. Read-side (``/me``, the receipt) renders the line iff
    # this is set; never invents a placeholder.
    price_usd_at_submit: float | None = None
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    parse_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """One row of the ranking query that backs ``GET /board``.

    Three numbers per source: hit rate, conviction-weighted PnL,
    bet count. The bot renders all three on each row; the API
    SORTS by an internal ``sort_score`` (Bayesian-shrunk PnL) so a
    lucky one-bet wonder can't top a multi-bet track record.

    The sort score itself is NOT carried on this DTO — it's an
    internal ranking key, not user-facing. ``rank`` is the rendered
    position (1-indexed) after the ORDER BY.

    ``accuracy_pct`` and ``weighted_pnl_pct`` are both floats here
    (not Optional) because the leaderboard query already filters
    cold-start sources OUT — every entry has at least one resolved
    outcome, so the cold-start NULLs from the underlying snapshot
    row are never observed at this layer.
    """

    rank: int
    display_name: str
    calls_resolved: int
    calls_correct: int
    accuracy_pct: float
    weighted_pnl_pct: float
    last_call_at: datetime | None = None


# ---------------------------------------------------------------------------
# Errors — repositories raise these instead of leaking SQLAlchemy types.
# ---------------------------------------------------------------------------


class RepositoryError(Exception):
    """Base for repository-layer errors."""


class SourceAlreadyExistsError(RepositoryError):
    """Raised when an INSERT collides with an existing source.

    Bot retries / duplicate-redeem are the most common cause. The
    application layer maps this to either a 409 or an idempotent
    "you're already onboarded" response.
    """


class InviteNotFoundError(RepositoryError):
    """Raised when a code lookup misses or finds an unredeemable invite."""


class InviteAlreadyRedeemedError(RepositoryError):
    """Raised by the atomic redeem path when a code was consumed first."""


# ---------------------------------------------------------------------------
# Protocols — repository contracts. Adapter implementations live in
# ``merlin_infrastructure.db.repositories``.
# ---------------------------------------------------------------------------


class SourceRepositoryPort(Protocol):
    """Persist + read ``sources`` rows.

    Sprint 1 surface is intentionally tiny — just enough for the bot's
    ``/start`` onboarding and the admin-bootstrap flow. The reputation
    write path (Admiralty updates) lands when the worker writes
    outcomes.
    """

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        """Return the source if it exists, else ``None``."""
        ...

    async def insert(
        self,
        *,
        source_id: str,
        display_name: str | None,
        kind: str,
        consent_version: str,
    ) -> SourceRecord:
        """INSERT a new source row. Raises ``SourceAlreadyExistsError`` on PK clash.

        Admiralty fields are set to their zeroed defaults
        (``reliability='F'``, ``credibility=6``) — the DB-level
        ``server_default`` does the work; the repository doesn't pass
        them explicitly.
        """
        ...

    async def update_display_name(self, source_id: str, display_name: str) -> SourceRecord:
        """Set the leaderboard display name. Idempotent on equal value."""
        ...


class SourceIdentityRepositoryPort(Protocol):
    """The PII repository — raw Telegram IDs (MVP §13, §15 invariant 9).

    Only the onboarding use case writes here; everything else looks up
    by ``source_id``. Future Phase-2 work adds a GDPR-export read path.
    """

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        """Reverse-lookup: from a raw TG id, return the opaque source_id.

        Used by the redeem and admin-bootstrap endpoints to spot
        already-onboarded users (idempotent re-redeems).
        """
        ...

    async def insert(
        self,
        *,
        source_id: str,
        tg_user_id: int,
        tg_handle: str | None,
    ) -> None:
        """INSERT a new identity row.

        Caller must hold a TX with the matching ``sources.insert`` so
        the two writes commit together.
        """
        ...


class InviteRepositoryPort(Protocol):
    """Persist + atomically redeem ``invites`` rows."""

    async def get_redeemable_by_code(self, code: str) -> InviteRecord | None:
        """Look up a code that is still consumable.

        Filters: ``redeemed_at IS NULL AND revoked_at IS NULL AND
        expires_at > now()``. Returns ``None`` on miss — callers must
        translate that to the 404 the bot expects.
        """
        ...

    async def insert(
        self,
        *,
        code: str,
        created_by_source_id: str | None,
        expires_at: datetime,
    ) -> InviteRecord:
        """INSERT a new invite. Raises if ``code`` collides (UNIQUE)."""
        ...

    async def mark_redeemed(
        self,
        *,
        invite_id: UUID,
        redeemed_by_source_id: str,
        redeemed_at: datetime,
    ) -> bool:
        """Atomically mark an invite redeemed.

        Implemented as ``UPDATE ... WHERE id = :id AND redeemed_at IS
        NULL`` and returns True iff exactly one row was updated. Two
        racing redeems for the same code will see one True and one
        False — the loser raises ``InviteAlreadyRedeemedError`` at the
        application layer.
        """
        ...

    async def revoke_by_code(
        self,
        *,
        code: str,
        revoked_at: datetime,
    ) -> bool:
        """Atomically revoke an unredeemed invite by code.

        Implemented as ``UPDATE ... WHERE code = :code AND redeemed_at
        IS NULL AND revoked_at IS NULL`` and returns True iff exactly
        one row was updated. False covers all three "code is not in a
        revokable state" cases — unknown code, already redeemed,
        already revoked — and the application layer translates that
        to the relevant 404 / 409.

        Expired codes are still revokable on purpose: a leaked
        soon-to-expire code is worth killing immediately rather than
        waiting on the clock.
        """
        ...

    async def get_inviter_tg_user_id_by_redeemed_source_id(
        self,
        redeemed_by_source_id: str,
    ) -> int | None:
        """Reverse-walk the redemption chain to the inviter's raw TG id.

        Used by ``PATCH /me`` to notify the inviter when their invitee
        finishes onboarding (first display-name set). Returns ``None``
        for every "no one to notify" shape so the caller can collapse
        them all into a single no-op branch:

        * Admin-bootstrapped user — no row in ``invites`` keyed on this
          source as redeemer.
        * Admin-issued invite where the admin had no ``sources`` row
          at the time the code was minted (``created_by_source_id`` is
          NULL on the invite row).
        * Inviter's ``source_identity`` row is missing (should not
          happen in production — the redeem path co-commits the
          identity row — but the column is FK-nullable so we treat the
          absence as "no notification target").

        When multiple redemptions key on the same invitee (not possible
        on the production path — the redeem route is idempotent — but
        the DB has no UNIQUE constraint on ``redeemed_by_source_id``),
        the adapter returns the inviter behind the oldest redemption.
        That choice keeps the "who originally invited them" answer
        deterministic and stable across calls.

        Implemented as a single ``SELECT`` joining
        ``invites → source_identity`` on the inviter's ``source_id`` so
        the application layer never sees raw Telegram IDs for anyone
        but the inviter being notified.
        """
        ...


class ClaimRepositoryPort(Protocol):
    """Persist + read ``claims`` rows.

    Sprint-1 slice: ``create`` (the ``/alpha`` Confirm write),
    ``list_open_by_source`` (``/me``'s open list), and
    ``count_resolved_by_source`` (``/me``'s resolved tally).

    The leaderboard query lives on
    ``SourceReputationRepositoryPort.leaderboard`` instead — it reads
    snapshot rows from ``source_reputation_history``, not raw claims,
    so coupling the read to the reputation repository keeps the
    layering honest.

    Append-only invariant (MVP §15 invariant 7): there is no
    ``update()`` / ``delete()`` method by design. Closed-loop columns
    (``edits`` JSONB, ``soft_deleted_at``) and their write methods land
    with the worker.
    """

    async def create(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        asset_coingecko_id: str,
        asset_symbol: str,
        asset_name: str | None,
        direction: str,
        horizon_seconds: int,
        conviction: str,
        thesis_text: str,
        asset_query: str | None,
        submitted_at: datetime,
        resolves_at: datetime,
        parsed_via: str,
        extracted_fields: dict[str, Any],
        parse_metadata: dict[str, Any],
        price_usd_at_submit: float | None = None,
    ) -> ClaimRecord:
        """INSERT a new claim row. Flushes, does not commit.

        ``price_usd_at_submit`` is the best-effort entry-price snapshot
        the API captures from CoinGecko at ``POST /claims`` time. The
        default is ``None`` so existing callers and tests don't need a
        synchronised update — pass it when you have it, omit when
        market data was unavailable.
        """
        ...

    async def list_open_by_source(self, source_id: str) -> list[ClaimRecord]:
        """Open calls for a given source.

        Filters ``WHERE source_id = :sid AND resolves_at > now()``,
        orders by ``resolves_at ASC`` so the soonest-to-resolve sits at
        the top of the list (most actionable for the user).
        """
        ...

    async def count_resolved_by_source(self, source_id: str) -> int:
        """Count of calls whose horizon has passed.

        ``WHERE source_id = :sid AND resolves_at <= now()``. Honest
        proxy for "resolved" until the outcomes table exists — by the
        time ``resolves_at`` is past, the call IS over even if it
        hasn't been scored yet.
        """
        ...


# ---------------------------------------------------------------------------
# OutcomeRepositoryPort — append-only outcome events.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """Read-side projection of a row in ``outcomes``."""

    id: UUID
    claim_id: UUID
    source_id: str
    status: str
    observed_return_pct: float | None
    is_correct: bool | None
    price_t0_usd: float | None
    price_resolve_usd: float | None
    price_t0_ref: str | None
    price_resolve_ref: str | None
    supersedes_outcome_id: UUID | None
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class ResolvedOutcomeForReputation:
    """One resolved outcome's view used by the reputation recompute.

    The recompute needs three things per row to build a snapshot:

    * ``is_correct`` — drives the hit-rate counts.
    * ``observed_return_pct`` + ``direction`` — combined into a
      signed directional return (positive when the call was right).
    * ``conviction`` — selects the per-bet stake multiplier
      (low=1x, med=3x, high=5x).

    Carrying ``direction`` and ``conviction`` on this record means
    the JOIN to ``claims`` lives in one repository method (clean SQL
    boundary) and every downstream computation stays in
    ``merlin_core.domain.scoring`` (pure functions, easy to test).
    """

    direction: str
    conviction: str
    observed_return_pct: float
    is_correct: bool


class OutcomeRepositoryPort(Protocol):
    """Persist + read ``outcomes`` rows.

    Append-only (MVP §15 invariants 7/8) — no ``update`` or ``delete``
    on this port; corrections INSERT a new row with
    ``supersedes_outcome_id`` pointing at the previous head.

    ``insert_resolved`` is the canonical happy path the resolver hits.
    ``insert_rejected_asset_mutation`` covers the Q-023 case where
    CoinGecko 404s an asset between t0 and t_resolve.
    """

    async def insert_resolved(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        observed_return_pct: float,
        is_correct: bool,
        price_t0_usd: float,
        price_resolve_usd: float,
        price_t0_ref: str,
        price_resolve_ref: str,
        resolved_at: datetime,
    ) -> OutcomeRecord:
        """INSERT a successful resolution event. Flushes; does not commit."""
        ...

    async def insert_rejected_asset_mutation(
        self,
        *,
        claim_id: UUID,
        source_id: str,
        resolved_at: datetime,
    ) -> OutcomeRecord:
        """INSERT a Q-023 rejection row. Flushes; does not commit."""
        ...

    async def latest_for_claim(self, claim_id: UUID) -> OutcomeRecord | None:
        """Head of the supersedes chain for the claim, or ``None`` if unresolved.

        Used by the resolver's idempotency guard: if a row already
        exists for ``claim_id``, ``resolve_outcome`` short-circuits
        (re-running a retried job never double-notifies the user).
        """
        ...

    async def count_resolved_and_correct_for_source(
        self,
        source_id: str,
    ) -> tuple[int, int]:
        """Return ``(resolved, correct)`` for the source.

        Filters ``status='resolved'`` only — Q-023 rejection rows and
        ``resolution_failed_final`` rows don't count against (or for)
        the user's reputation. The "wash_zero" v0 choice means flat
        outcomes are in ``resolved`` but NOT in ``correct``.

        Lightweight alternative to
        ``fetch_resolved_for_reputation`` when the caller only needs
        the counts (no PnL math). The recompute itself prefers the
        richer fetch; this method stays on the port for callers that
        don't need direction / conviction.
        """
        ...

    async def fetch_resolved_for_reputation(
        self,
        source_id: str,
    ) -> list[ResolvedOutcomeForReputation]:
        """Return one row per resolved outcome with direction + conviction.

        Used by ``recompute_source_reputation`` to compute the
        conviction-weighted PnL and the Bayesian-shrunk sort score.
        Single SELECT with a JOIN ``outcomes → claims`` for the per-bet
        direction + conviction (which live on the claim, not the
        outcome). Filters ``outcomes.status='resolved'`` so Q-023
        rejection rows and ``resolution_failed_final`` rows stay out
        of the PnL — same filter as ``count_resolved_and_correct_for_source``.

        ``direction``, ``conviction``, and ``observed_return_pct`` are
        non-NULL on resolved outcomes by CHECK constraint, so the
        record fields are non-Optional. Rows that fail those CHECKs
        could never have been INSERTed in the first place; the
        resolver enforces them at write time.
        """
        ...


# ---------------------------------------------------------------------------
# SourceReputationRepositoryPort — append-only reputation snapshots.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceReputationRecord:
    """Read-side projection of a row in ``source_reputation_history``.

    Three layered views of the same history:

    * ``accuracy_pct`` — plain hit rate, the boolean view.
    * ``weighted_pnl_pct`` — conviction-weighted signed PnL %,
      the magnitude view.
    * ``sort_score`` — Bayesian-shrunk PnL, the ranking view (NOT
      rendered to users).

    All three follow the same NULL contract for cold-start sources:
    ``accuracy_pct`` / ``weighted_pnl_pct`` are ``None`` when
    ``outcomes_resolved == 0``, but ``sort_score`` is ``0.0`` (sorts
    to the middle of the pack, not to ``-inf``).
    """

    id: UUID
    source_id: str
    outcomes_resolved: int
    outcomes_correct: int
    accuracy_pct: float | None
    weighted_pnl_pct: float | None
    sort_score: float
    triggering_outcome_id: UUID | None
    effective_at: datetime


class SourceReputationRepositoryPort(Protocol):
    """Persist + read ``source_reputation_history`` rows.

    Append-only (MVP §15 invariant 10): every recompute INSERTs a new
    row. Readers ask for ``latest_for_source(source_id)`` to project
    "what is this source's reputation right now". The leaderboard
    query is exposed here instead of on ``ClaimRepositoryPort`` so the
    reading layer joins reputation snapshots, not raw claims.
    """

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
        """INSERT one history row. Flushes; does not commit.

        ``weighted_pnl_pct`` and ``accuracy_pct`` must BOTH be ``None``
        or BOTH be set — the DB CHECK ``ck_srh_pnl_null_iff_cold_start``
        plus ``ck_srh_accuracy_null_iff_cold_start`` together enforce
        "cold-start sources have neither, resolved sources have both".
        ``sort_score`` is always a float (``0.0`` cold-start).
        """
        ...

    async def latest_for_source(
        self,
        source_id: str,
    ) -> SourceReputationRecord | None:
        """``ORDER BY effective_at DESC LIMIT 1`` for one source.

        Returns ``None`` for a cold-start source (no resolutions yet).
        Readers map ``None`` → "tracking your first resolutions" in
        bot-voice terms.
        """
        ...

    async def leaderboard(self, window: str) -> list[LeaderboardEntry]:
        """Top sources by latest snapshot's ``sort_score``.

        ``window`` is accepted for forward compatibility; v0 ignores it
        and returns the all-time ranking — there's no time-windowed
        slicing of reputation snapshots yet. Cold-start sources are
        omitted (their ``accuracy_pct`` and ``weighted_pnl_pct`` are
        NULL). Ranking key is ``sort_score`` (Bayesian-shrunk PnL),
        NOT the raw hit rate — locked by the design conversation.
        """
        ...
