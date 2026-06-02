"""Pure outcome-scoring helpers — one definition of "did the call hit?".

Two questions every consumer of an ``observed_return_pct`` needs to
answer, in exactly one place:

* **Was the move large enough to count?** ``is_flat`` — magnitude
  below the flat threshold counts as a wash regardless of sign.
* **Was the directional call correct?** ``is_correct_call`` — only
  True when the move cleared the flat threshold AND the sign matches
  the user's stated direction.

Two consumers today:

* The telegram outcome templates (won / lost / flat dispatch).
* The reputation recompute job (count "correct" rows for hit rate).

A single helper guarantees both views agree. The Sprint-1 regression
the user wants pinned in CI is "the notification said 'you called it
right' but the leaderboard didn't credit me" — that can only happen
if the two computations drift, and they only drift if they live in
two files.

User-locked v0 reputation choices (matching the conversation):

* ``flat_handling = wash_zero``: a flat outcome IS resolved but is NOT
  correct. Counts in the denominator, not the numerator.
* ``min_calls_for_rank = show_all``: there's no cold-start gate here
  — surfacing decisions live with the ``/board`` and ``/me`` readers.

This module is stdlib-only (domain rule) so it's importable from
``merlin_infrastructure`` (templates), ``merlin_worker`` (recompute),
and the API layer alike. The bot is not allowed to import
``merlin_core.domain`` — and doesn't need to, since the bot only
displays pre-computed outcomes.
"""

from __future__ import annotations

from typing import Final

# A move below this percentage counts as a wash for both notification
# framing and reputation scoring. 0.5% mirrors the telegram template's
# historical threshold; pinning it here as the single source means
# changing the bar in one place updates both views together.
FLAT_THRESHOLD_PCT: Final[float] = 0.5


def is_flat(observed_return_pct: float, *, flat_threshold_pct: float = FLAT_THRESHOLD_PCT) -> bool:
    """True iff ``|pct|`` is below the flat threshold (a wash).

    ``flat_threshold_pct`` is kw-only and exposed for future tuning;
    every caller in the v0 codebase passes the default. Override only
    when a test needs to probe edge cases at a different cutoff.
    """
    return abs(observed_return_pct) < flat_threshold_pct


def is_correct_call(
    direction: str,
    observed_return_pct: float,
    *,
    flat_threshold_pct: float = FLAT_THRESHOLD_PCT,
) -> bool:
    """True iff the user's directional call was correct AND not flat.

    Semantics (matches the telegram dispatch in ``render_outcome``):

    * ``|pct| < flat`` → False (wash counts as resolved but not correct).
    * ``direction == "long"`` → True iff ``pct > 0``.
    * ``direction == "short"`` → True iff ``pct < 0``.

    Unknown directions return False so a corrupted ``claims.direction``
    row never silently inflates a source's hit rate.
    """
    if is_flat(observed_return_pct, flat_threshold_pct=flat_threshold_pct):
        return False
    if direction == "long":
        return observed_return_pct > 0
    if direction == "short":
        return observed_return_pct < 0
    return False


def accuracy_pct(outcomes_correct: int, outcomes_resolved: int) -> float | None:
    """Plain hit rate as a percent. ``None`` if no outcomes resolved yet.

    Defining accuracy here (instead of inline in the resolver / reader)
    keeps the "what is reputation at v0" answer in one file alongside
    the correctness rule. Returns ``None`` rather than ``0.0`` on the
    cold-start case — readers (``/me``, ``/board``) need to tell
    "no history yet" from "a real 0% hit rate", and a float column
    can't carry that signal.
    """
    if outcomes_resolved <= 0:
        return None
    return 100.0 * outcomes_correct / outcomes_resolved
