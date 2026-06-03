"""``GET /me/history`` — resolved-call history, newest first.

Paginated via ``offset`` + ``limit`` on the wire, with an additional
``as_of`` ceiling that freezes the view at first-page time. The first
``/history`` request omits ``as_of`` and the server stamps it with
``datetime.now(UTC)``; subsequent "Show 30 more" requests round-trip
the same ``as_of`` so a new outcome landing mid-session can't shift
already-paged rows down into the next page (which would duplicate the
boundary row).

The response carries ``has_more`` + ``next_offset`` + ``as_of`` so the
bot stays stateless: the inline button encodes (as_of, offset) in its
callback_data and re-sends them verbatim on the next call.

Derivation rule:
``has_more = (offset + len(entries) < total_resolved_as_of_cutoff)``,
``next_offset = offset + limit if has_more else None``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from merlin_core.contracts.horizons import HORIZON_MAX_SECONDS, HORIZON_MIN_SECONDS

#: Default page size for ``/me/history`` — kept here so the API
#: route, the client, the bot's inline-button label, and the bot's
#: page-size constant all reference one source of truth. Changing it
#: in one place updates the wire default, the button label, and the
#: bot's request shape together.
HISTORY_DEFAULT_PAGE_SIZE: int = 30

#: Upper bound on a single page so a misbehaving client cannot drain
#: the database in one request. Enforced via ``Query(le=...)`` on the
#: route.
HISTORY_MAX_PAGE_SIZE: int = 100


class HistoryEntry(BaseModel):
    """One row in the user's resolved-call history list.

    ``observed_return_pct`` is the raw signed move (positive when
    the market went up, regardless of the user's direction).

    ``is_correct`` and ``is_flat`` are both server-side
    classifications, derived via ``merlin_core.domain.scoring`` so
    the icon set rendered here always matches the live resolution
    notification the user already saw. Encoding them on the wire
    (rather than recomputing on the bot side) keeps the bot inside
    its layer rule — bot is allowed ``merlin_client`` +
    ``merlin_core.contracts`` only, NOT ``merlin_core.domain``.

    Three combinations the bot dispatches on:

    * ``is_correct=True`` → won (🎉). ``is_flat`` is always False
      here by construction (``is_correct_call`` returns False on
      flats).
    * ``is_correct=False, is_flat=True`` → flat / wash (📝).
    * ``is_correct=False, is_flat=False`` → lost (🙂‍↔️).
    """

    claim_id: UUID
    asset_symbol: str
    direction: str
    horizon_seconds: int = Field(
        ...,
        ge=HORIZON_MIN_SECONDS,
        le=HORIZON_MAX_SECONDS,
        description="Horizon in seconds; integer; 1h..90d range.",
    )
    observed_return_pct: float
    is_correct: bool
    is_flat: bool
    resolved_at: datetime


class HistoryResponse(BaseModel):
    """``GET /me/history`` response payload.

    Pagination breadcrumbs:

    * ``has_more`` — true iff there are more resolved outcomes past
      the current page (within the frozen ``as_of`` window). The bot
      renders the "Show 30 more" button only on this branch.
    * ``next_offset`` — offset the next request should pass.
      ``None`` when no more rows are available — pairs with
      ``has_more=False``.
    * ``as_of`` — frozen window ceiling. The server stamps this at
      first-page time (caller omits the query param) and echoes the
      caller-supplied value on subsequent pages. The bot **must**
      pass it back on follow-up requests; otherwise an outcome that
      resolves between pages would shift every row downward and the
      "Show 30 more" click would dup the previous page's last row.

    Computed server-side as
    ``has_more = (offset + len(entries) < total_resolved_at_or_before_as_of)``,
    ``next_offset = offset + limit if has_more else None``.
    """

    entries: list[HistoryEntry] = Field(default_factory=list)
    has_more: bool = False
    next_offset: int | None = None
    as_of: datetime = Field(
        ...,
        description=(
            "Frozen window ceiling. Outcomes resolving after this "
            "timestamp are excluded from the paginated view so the "
            "bot's 'Show 30 more' click never duplicates a boundary "
            "row. The bot round-trips this value on follow-up "
            "requests."
        ),
    )
