"""Horizon helpers — continuous seconds, parser, humaniser.

Horizons cross every layer (the bot parses user text, the API
validates an ``int`` payload, the worker / notification templates
render the value back as a human label), so the helpers that
encode the wire-format contract live here in
``merlin_core.contracts`` rather than in ``merlin_core.domain``.
That keeps the strict layer rules (the bot is allowed
``merlin_client + merlin_core.contracts`` only) satisfied while
giving every consumer a single source of truth.

The functions are stdlib-only (no Pydantic) — they describe how a
``horizon_seconds`` field on a contract DTO is interpreted, not how
business rules score it. MVP §15 invariant 4 ("Horizons are exact
seconds. No business-day math, no calendar math, no DST.") still
holds by construction: we only ever do ``timedelta(seconds=N)``
arithmetic on the result.
"""

from __future__ import annotations

import re
from datetime import timedelta

HORIZON_MIN_SECONDS: int = 3_600  # 1 hour
HORIZON_MAX_SECONDS: int = 7_776_000  # 90 days

# Segmentation boundary for the ``/board`` "Day Traders" vs
# "Swing Traders" filter. A claim with ``horizon_seconds <=
# HORIZON_SHORT_TERM_MAX_SECONDS`` is classified as short-term
# ("Day Traders" = intraday by the SEC PDT convention); anything
# strictly greater is long-term ("Swing Traders"). Public because
# the leaderboard repo + bot rendering both import it; deliberately
# distinct from the private ``_HOURS_TO_DAYS_THRESHOLD_HOURS``
# above, which is a humaniser display threshold (hours-vs-days
# rendering at 48h), NOT a domain segmentation rule. Widening this
# to 48h (172_800) is a one-line change with no migration thanks
# to the on-the-fly leaderboard slice in
# ``SourceReputationRepository.leaderboard``.
HORIZON_SHORT_TERM_MAX_SECONDS: int = 86_400  # 24 hours

# Unit boundaries used by ``humanise_horizon`` to pick the cleanest
# representation. Named here so the comparisons read intentionally
# (``HOURS_TO_DAYS_THRESHOLD_HOURS`` rather than a bare ``48``) and
# tooling (``ruff PLR2004``) doesn't flag them as magic numbers.
_HOURS_TO_DAYS_THRESHOLD_HOURS: int = 48
_DAYS_TO_MONTHS_THRESHOLD_DAYS: int = 60
_DAYS_PER_MONTH: int = 30
_DAYS_PER_WEEK: int = 7

# Shape of the word-form horizon ("two days", "a week") — exactly
# two tokens (count + unit). Named so the ``len(tokens) == _PAIR``
# comparison in the parser reads as intent, not a magic number.
_PAIR: int = 2

# Quick-pick presets surfaced as buttons in the bot's horizon picker.
# Free-form text reply ("3 hours", "2 weeks") is the always-available
# alternative; presets are convenience for the common cases.
HORIZON_QUICK_PICKS: tuple[int, ...] = (
    86_400,  # 24 hours
    259_200,  # 3 days
    604_800,  # 1 week
    1_209_600,  # 2 weeks
    2_592_000,  # 1 month
    7_776_000,  # 3 months
)


class HorizonParseError(ValueError):
    """Raised when ``parse_horizon_text`` can't interpret the input.

    The bot catches this and replies with the format hint
    ("Try '3 hours', '2 weeks', '30 days'...") so the user can
    correct without leaving the picker.
    """


class HorizonRangeError(ValueError):
    """Raised when a parsed horizon is outside ``[MIN, MAX]``.

    Distinct from ``HorizonParseError`` so the bot can render the
    range-specific hint ("between 1 hour and 90 days") instead of
    the generic format hint.
    """


# Unit → seconds. Keys are the canonical singular nouns we accept;
# the regex below tolerates short forms (``h``, ``d``, ``w``) and
# plurals (``hours``, ``days``).
_UNIT_SECONDS: dict[str, int] = {
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,  # 30 days flat — MVP §15 invariant 4 (no calendar math)
    "year": 31_536_000,  # 365 days flat
}

# Short forms map to canonical unit names. ``m`` is deliberately
# excluded because it's ambiguous (minute / month); users must say
# ``min``, ``minute`` or ``mo``, ``month``. ``y`` covers ``year``.
_UNIT_ALIASES: dict[str, str] = {
    "min": "minute",
    "minutes": "minute",
    "h": "hour",
    "hr": "hour",
    "hrs": "hour",
    "hours": "hour",
    "d": "day",
    "days": "day",
    "w": "week",
    "wk": "week",
    "wks": "week",
    "weeks": "week",
    "mo": "month",
    "mos": "month",
    "months": "month",
    "y": "year",
    "yr": "year",
    "yrs": "year",
    "years": "year",
}

# Word-form numbers we accept in lieu of a digit. ``a`` / ``an`` are
# folded to "one" so colloquial phrasings ("a week", "an hour") parse
# the same as ``"1 week"`` / ``"1 hour"``. Capped at twelve — past
# that users are expected to type digits, and keeping the table small
# stops it from swallowing typos like "twentythree" as a valid count.
_WORD_NUMBERS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

# Trailing punctuation a user instinctively types at the end of a
# chat reply ("4 days.", "3 hours?", "2 weeks,"). We strip these so
# the structural match doesn't see them — keeps the regex simple
# while accepting the common typing habit.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)\]]+$")

# ``<number> <unit>`` — e.g. ``"3 hours"``, ``"1.5 days"``, ``"2w"``.
# Whitespace between number and unit is optional so ``"3h"`` works,
# and ``\s+`` (rather than a single ``\s``) tolerates accidental
# double-spaces.
_NUMERIC_RE = re.compile(
    r"^(?P<n>\d+(?:\.\d+)?)\s*(?P<u>[A-Za-z]+)$",
)


def parse_horizon_text(text: str) -> int:
    """Parse a free-form horizon string into seconds.

    Friendly forms the user is likely to type all parse cleanly:
    ``"3 hours"``, ``"3h"``, ``"1.5d"``, ``"2 weeks"``, ``"30min"``,
    ``"6 months"``. The parser is intentionally tolerant of common
    typing habits so the bot accepts natural chat phrasing instead
    of bouncing the user back to the quick-pick keyboard:

    - Trailing sentence punctuation: ``"4 days."`` parses as
      ``"4 days"``.
    - Mixed case: ``"3 HOURS"`` parses identically to ``"3 hours"``.
    - Collapsed / extra whitespace: ``"  3   hours  "`` is fine.
    - Bare units: ``"week"`` reads as "one week", ``"hour"`` as
      "one hour".
    - Articles: ``"a week"`` / ``"an hour"`` fold to count=1.
    - Word numbers up to twelve: ``"two days"``, ``"three hours"``,
      ``"twelve weeks"``.

    Raises ``HorizonParseError`` on unparseable input (vague phrases
    like ``"soon"`` / ``"tomorrow"``, unknown units, multi-clause
    phrases) and ``HorizonRangeError`` when the parsed horizon is
    outside ``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]``. The
    split lets the bot render two distinct hints rather than one
    generic "didn't get that" message.
    """
    if not isinstance(text, str):
        raise HorizonParseError(f"horizon must be a string, got {type(text).__name__}")
    normalised = _normalise_horizon_text(text)
    if not normalised:
        raise HorizonParseError(
            f"can't read {text!r} as a horizon — try '3 hours', '2 weeks', '30 days'"
        )
    n, raw_unit = _split_count_and_unit(text, normalised)
    canonical = _UNIT_ALIASES.get(raw_unit, raw_unit)
    if canonical not in _UNIT_SECONDS:
        raise HorizonParseError(
            f"don't know the unit {raw_unit!r} — try hours, days, weeks, months"
        )
    if n <= 0:
        raise HorizonParseError(f"horizon must be positive, got {n}")
    seconds = round(n * _UNIT_SECONDS[canonical])
    if seconds < HORIZON_MIN_SECONDS or seconds > HORIZON_MAX_SECONDS:
        raise HorizonRangeError(
            f"{text!r} is outside the allowed range "
            f"(1 hour to 90 days) — got {humanise_horizon(seconds)}"
        )
    return seconds


def _normalise_horizon_text(text: str) -> str:
    """Lowercase, trim whitespace and trailing sentence punctuation.

    Keeps internal whitespace as-is (``"two days"`` must stay two
    tokens) but folds everything that's irrelevant for the
    structural match: leading/trailing whitespace plus the
    period / comma / question-mark a user typically tacks onto the
    end of a chat reply.
    """
    s = text.strip().lower()
    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    # Collapse runs of internal whitespace so ``"two   days"`` and
    # ``"two days"`` parse identically; ``str.split()`` would also
    # do this implicitly, but doing it here keeps the numeric-regex
    # path (``"  3   hours"``) consistent too.
    s = " ".join(s.split())
    return s


def _split_count_and_unit(original: str, normalised: str) -> tuple[float, str]:
    """Return ``(count, raw_unit)`` for the normalised input.

    Three accepted shapes, in priority order:

    1. ``<digits> <unit>`` (``"3 hours"``, ``"1.5d"``, ``"30min"``)
       — the fast path; what the LLM emits and what most users type.
    2. ``<word-number> <unit>`` (``"two days"``, ``"an hour"``);
       articles ``"a"`` / ``"an"`` fold to count=1.
    3. ``<unit>`` alone (``"week"``, ``"hour"``) — read as count=1.

    Anything else (multi-clause phrases, unknown leading word,
    bare numbers without a unit) raises ``HorizonParseError``.
    We surface the *original* input in the error message so the
    user sees the string they typed, not our normalised view.
    """
    match = _NUMERIC_RE.match(normalised)
    if match is not None:
        return float(match.group("n")), match.group("u")
    tokens = normalised.split()
    if len(tokens) == 1:
        return 1.0, tokens[0]
    if len(tokens) == _PAIR and tokens[0] in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[tokens[0]]), tokens[1]
    raise HorizonParseError(
        f"can't read {original!r} as a horizon — try '3 hours', '2 weeks', '30 days'"
    )


def humanise_horizon(seconds: int) -> str:
    """Render an integer-second horizon as a user-facing label.

    Bot voice rule: never show ``"7d"`` / ``"86400s"`` to a user.
    Picks the largest unit that divides cleanly, then falls back to
    the next-smaller unit for fractional values. ``humanise_horizon(
    3600)`` → ``"1 hour"``; ``humanise_horizon(10_800)`` → ``"3
    hours"``; ``humanise_horizon(86_400)`` → ``"24 hours"`` (not
    ``"1 day"`` — the user typed it that way historically and "24
    hours" reads cleaner for short calls).

    Stable for all inputs in ``[1, HORIZON_MAX_SECONDS]``; outside
    that range we still produce *some* string (e.g. for tests or
    historical rows) rather than raise.
    """
    if seconds <= 0:
        return f"{seconds} seconds"
    # ``24h`` is the historical "1 day" boundary; the original enum
    # spelled it as hours, the bot's outcome templates expect "24
    # hours", and users naturally read short calls in hours. Keep
    # that convention up to 47 hours, switch to days at 48h+.
    if seconds < _HOURS_TO_DAYS_THRESHOLD_HOURS * 3_600:
        return _humanise_sub_two_days(seconds)
    return _humanise_two_days_plus(seconds)


def _humanise_sub_two_days(seconds: int) -> str:
    """Hours / minutes representation for sub-48h horizons."""
    if seconds % 3_600 == 0:
        n = seconds // 3_600
        return f"{n} hour" if n == 1 else f"{n} hours"
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute" if n == 1 else f"{n} minutes"
    return f"{seconds} seconds"


def _humanise_two_days_plus(seconds: int) -> str:
    """Days / weeks / months representation. Pick the cleanest unit.

    Render multi-of-30 days as months when it's a clean count AND
    >= 60 days (so "60 days" → "2 months", "90 days" → "3 months";
    but "30 days" stays as days since users sometimes mean "this
    month" and sometimes mean "30 days exactly" — we don't auto-
    collapse the ambiguous case).
    """
    if seconds % (_DAYS_PER_WEEK * 86_400) == 0:
        n = seconds // (_DAYS_PER_WEEK * 86_400)
        return f"{n} week" if n == 1 else f"{n} weeks"
    if seconds % 86_400 == 0:
        n = seconds // 86_400
        if n >= _DAYS_TO_MONTHS_THRESHOLD_DAYS and n % _DAYS_PER_MONTH == 0:
            m = n // _DAYS_PER_MONTH
            return f"{m} month" if m == 1 else f"{m} months"
        return f"{n} day" if n == 1 else f"{n} days"
    # Sub-day fractional value over 48h — odd but representable.
    hours = seconds // 3_600
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


def horizon_to_timedelta(seconds: int) -> timedelta:
    """Return the ``timedelta`` for a horizon in seconds.

    Thin wrapper kept so callers reading ``submitted_at +
    horizon_to_timedelta(claim.horizon_seconds)`` are explicit about
    the conversion rather than reaching for ``timedelta`` directly.
    """
    return timedelta(seconds=seconds)


def is_in_range(seconds: int) -> bool:
    """True iff ``seconds`` lies within ``[MIN, MAX]`` inclusive."""
    return HORIZON_MIN_SECONDS <= seconds <= HORIZON_MAX_SECONDS
