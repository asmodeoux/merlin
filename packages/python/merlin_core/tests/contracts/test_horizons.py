"""Unit tests for the continuous-horizon helpers (MVP §15 invariant 4).

Horizon storage flipped from a quantised Literal (``"1h"`` / ``"7d"``
/ ...) to integer seconds in ``[HORIZON_MIN_SECONDS,
HORIZON_MAX_SECONDS]``. The invariant ("exact seconds, no
calendar/DST math") is preserved by construction — ``int`` seconds
are inherently free of calendar / DST drift.

These tests pin:

- The bound constants match the documented 1h / 90d window.
- ``parse_horizon_text`` accepts the friendly forms a user is likely
  to type (``"3 hours"``, ``"2 weeks"``, ``"1.5d"``, ``"30min"``).
- Out-of-range and unparseable inputs raise the *distinct* error
  types so the bot can render a useful hint.
- ``humanise_horizon`` is the inverse of the parser for the values
  the picker surfaces, and never leaks raw codes / unit suffixes.
- ``horizon_to_timedelta`` round-trips cleanly.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from merlin_core.contracts.horizons import (
    HORIZON_MAX_SECONDS,
    HORIZON_MIN_SECONDS,
    HORIZON_QUICK_PICKS,
    HORIZON_SHORT_TERM_MAX_SECONDS,
    HorizonParseError,
    HorizonRangeError,
    horizon_to_timedelta,
    humanise_horizon,
    is_in_range,
    parse_horizon_text,
)

# --- bounds -----------------------------------------------------------------


def test_min_is_one_hour() -> None:
    assert HORIZON_MIN_SECONDS == 3_600


def test_max_is_ninety_days() -> None:
    assert HORIZON_MAX_SECONDS == 90 * 86_400


def test_short_term_boundary_is_24_hours() -> None:
    """The ``/board`` "Day Traders" cutoff is exactly 24h in seconds.

    Pinned here so a future reviewer thinking of widening it sees
    the deliberate canonical-day-trader choice (and not a 48h
    accidental match to ``_HOURS_TO_DAYS_THRESHOLD_HOURS``).
    """
    assert HORIZON_SHORT_TERM_MAX_SECONDS == 86_400
    assert HORIZON_SHORT_TERM_MAX_SECONDS == 24 * 3_600


def test_short_term_boundary_lies_within_horizon_range() -> None:
    """Sanity: the segmentation boundary is a legal horizon value."""
    assert HORIZON_MIN_SECONDS <= HORIZON_SHORT_TERM_MAX_SECONDS <= HORIZON_MAX_SECONDS


def test_quick_picks_are_all_inside_range_and_sorted() -> None:
    """The quick-pick presets must be in-range and ordered shortest-first."""
    assert tuple(sorted(HORIZON_QUICK_PICKS)) == HORIZON_QUICK_PICKS
    for seconds in HORIZON_QUICK_PICKS:
        assert is_in_range(seconds), f"quick pick {seconds}s outside [MIN, MAX]"


# --- parse — happy path -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 hour", 3_600),
        ("3 hours", 10_800),
        ("24 hours", 86_400),
        ("3 days", 259_200),
        ("1 week", 604_800),
        ("2 weeks", 1_209_600),
        ("30 days", 2_592_000),
        ("1 month", 2_592_000),
        ("3 months", 7_776_000),
        ("90 days", 7_776_000),
    ],
)
def test_parse_horizon_text_friendly_phrases(text: str, expected: int) -> None:
    """The forms a user is most likely to type must all parse cleanly."""
    assert parse_horizon_text(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3h", 10_800),
        ("3H", 10_800),
        ("1d", 86_400),
        ("2w", 1_209_600),
        ("90d", 7_776_000),
        ("1.5d", 129_600),
        ("60min", 3_600),
        (" 3 hours ", 10_800),
    ],
)
def test_parse_horizon_text_short_forms(text: str, expected: int) -> None:
    """Short forms (``h`` / ``d`` / ``w``) and surrounding whitespace are OK."""
    assert parse_horizon_text(text) == expected


# --- parse — error paths ----------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "soon", "next week", "tomorrow", "abc"])
def test_parse_horizon_text_unparseable_raises_parse_error(text: str) -> None:
    """Garbage / vague phrases must raise ``HorizonParseError`` so the bot
    renders the "try '3 hours', '2 weeks', '30 days'" hint."""
    with pytest.raises(HorizonParseError):
        parse_horizon_text(text)


def test_parse_horizon_text_ambiguous_m_is_rejected() -> None:
    """Bare ``m`` is rejected because it's ambiguous (minute vs month)."""
    with pytest.raises(HorizonParseError):
        parse_horizon_text("3m")


def test_parse_horizon_text_negative_is_rejected() -> None:
    """A negative number is a typo / nonsense — reject with a parse error."""
    with pytest.raises(HorizonParseError):
        parse_horizon_text("-3 hours")


def test_parse_horizon_text_below_min_raises_range_error() -> None:
    """``30 minutes`` is parseable but outside the allowed window."""
    with pytest.raises(HorizonRangeError):
        parse_horizon_text("30 minutes")


def test_parse_horizon_text_above_max_raises_range_error() -> None:
    """``1 year`` is parseable but past the 90-day ceiling."""
    with pytest.raises(HorizonRangeError):
        parse_horizon_text("1 year")


# --- parse — lenient typing forms ------------------------------------------
#
# The parser is intentionally tolerant of natural chat phrasing — users
# don't type bot-shaped DSLs, they type sentences. Each parametrised
# case here was either:
#
# - directly requested by the user ("1 hour", "2h", "3 hours", "4 days.",
#   " 5 day ", "week", "two days"), or
# - a near-neighbour typo that would frustrate the user if it bounced
#   them back to the keyboard (mixed case, trailing punctuation,
#   collapsed whitespace, articles, bare short-form units).


@pytest.mark.parametrize(
    "text,expected",
    [
        # Trailing sentence punctuation — users instinctively type a
        # noun + period when answering "how long?".
        ("4 days.", 4 * 86_400),
        ("5 days!", 5 * 86_400),
        ("3 hours?", 10_800),
        ("2 weeks,", 1_209_600),
        ("3h.", 10_800),
        ("24h;", 86_400),
        # Surrounding / collapsed whitespace.
        (" 5 day ", 5 * 86_400),
        ("  3   hours  ", 10_800),
        ("\t2 weeks\n", 1_209_600),
        # Mixed case — the bot lower-cases before matching.
        ("3 HOURS", 10_800),
        ("3 Hours", 10_800),
        ("3 H", 10_800),
        ("Two Days.", 2 * 86_400),
        ("AN HOUR", 3_600),
        # Bare unit = "one unit" — colloquial shorthand.
        ("week", 604_800),
        ("hour", 3_600),
        ("day", 86_400),
        ("month", 2_592_000),
        # Bare short-form unit.
        ("h", 3_600),
        ("d", 86_400),
        ("w", 604_800),
        ("hr", 3_600),
        # Article + unit ("a week", "an hour") = one unit. The article
        # doesn't need to agree with the unit grammatically — we
        # accept "a hour" / "an day" because the user is typing fast,
        # not writing prose.
        ("a week", 604_800),
        ("an hour", 3_600),
        ("a day", 86_400),
        ("a month", 2_592_000),
        ("an day", 86_400),
        # Word numbers up to twelve.
        ("one hour", 3_600),
        ("two days", 2 * 86_400),
        ("three weeks", 3 * 604_800),
        ("five days", 5 * 86_400),
        ("ten days", 10 * 86_400),
        ("twelve weeks", 12 * 604_800),
        # Decimal fractions still work via the numeric path.
        ("0.5 days", 43_200),
        ("2.5 days", 216_000),
    ],
)
def test_parse_horizon_text_lenient_typing_forms(text: str, expected: int) -> None:
    """Common typing habits — trailing punctuation, mixed case, bare
    unit, articles, word numbers — all parse cleanly. A user who
    types "4 days." or "two days" must not be bounced back to the
    quick-pick keyboard with a generic "didn't get that" hint."""
    assert parse_horizon_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Word numbers beyond twelve — we deliberately don't maintain
        # that table; the bot tells the user to retype with digits.
        "twenty days",
        "fifteen hours",
        "thirty days",
        # Symbolic / vague phrases that don't anchor on a number.
        "asap",
        "now",
        "immediately",
        "later",
        # Multi-clause phrasing and ranges — too ambiguous to commit
        # to a single integer; the user is told to pick one form.
        "few days",
        "couple of days",
        "around 3 hours",
        "about 2 weeks",
        "2 hours and 30 minutes",
        "between 1 and 2 weeks",
        # Calendar-relative phrasing (would need calendar math the
        # MVP forbids — invariant 4).
        "by tomorrow",
        "this week",
        # Unit typos not covered by the alias table.
        "3 dayys",
        "3 wekes",
        "2 hrss",
        "5 dais",
        # Article / word number alone — no unit, nothing to anchor.
        "a",
        "an",
        "two",
        "twelve",
        # Bare digits without a unit.
        "3",
        "24",
        # Hyphen / slash separators in numbers we don't try to read.
        "1/2 day",
        "2-3 days",
    ],
)
def test_parse_horizon_text_extended_unparseable(text: str) -> None:
    """Phrases the parser intentionally refuses — too vague, too
    multi-clause, unknown unit, or word number outside the
    one..twelve range. The bot renders the format hint so the user
    retypes with a friendly form."""
    with pytest.raises(HorizonParseError):
        parse_horizon_text(text)


def test_parse_horizon_text_zero_count_is_rejected() -> None:
    """``"0 hours"`` parses structurally but ``0`` isn't a positive
    horizon — surface the parse-error hint so the user retypes."""
    with pytest.raises(HorizonParseError):
        parse_horizon_text("0 hours")
    with pytest.raises(HorizonParseError):
        parse_horizon_text("0 days")


@pytest.mark.parametrize(
    "text",
    [
        # Bare unit that lands above the 90-day ceiling (1 year).
        "year",
        "y",
        # Bare unit that lands below the 1-hour floor (1 minute).
        "minute",
        "min",
        # Article / word number + below-min unit.
        "a minute",
        "one min",
        # Word number + above-max unit.
        "two years",
        "three years",
    ],
)
def test_parse_horizon_text_lenient_forms_still_range_gated(text: str) -> None:
    """Lenient forms still go through the range gate — a user typing
    ``"year"`` alone gets the range hint, not a silently-accepted
    value above the 90-day ceiling."""
    with pytest.raises(HorizonRangeError):
        parse_horizon_text(text)


def test_parse_and_range_errors_are_distinct_subclasses() -> None:
    """Both inherit ``ValueError`` but are distinct types so the bot's
    handler can branch on which hint to render."""
    assert issubclass(HorizonParseError, ValueError)
    assert issubclass(HorizonRangeError, ValueError)
    assert not issubclass(HorizonParseError, HorizonRangeError)
    assert not issubclass(HorizonRangeError, HorizonParseError)


# --- humanise ---------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (3_600, "1 hour"),
        (10_800, "3 hours"),
        (86_400, "24 hours"),
        (259_200, "3 days"),
        (604_800, "1 week"),
        (1_209_600, "2 weeks"),
        (2_592_000, "30 days"),  # ambiguous "this month" vs "30 days"; keep as days
        (5_184_000, "2 months"),
        (7_776_000, "3 months"),
    ],
)
def test_humanise_horizon_renders_user_facing_label(seconds: int, expected: str) -> None:
    assert humanise_horizon(seconds) == expected


def test_humanise_horizon_never_leaks_raw_codes() -> None:
    """Bot voice: never show ``"7d"`` or ``"86400s"`` to a user."""
    for seconds in HORIZON_QUICK_PICKS:
        label = humanise_horizon(seconds)
        assert "d" not in label or "day" in label or "days" in label
        assert label != str(seconds)


def test_humanise_is_inverse_of_parse_for_friendly_phrases() -> None:
    """``parse_horizon_text(humanise_horizon(s)) == s`` for the quick picks
    — pins the public picker presets round-trip cleanly through both
    helpers (so a user reading "1 week" on the preview, retyping
    "1 week" into the picker, gets the same seconds back)."""
    for s in HORIZON_QUICK_PICKS:
        assert parse_horizon_text(humanise_horizon(s)) == s


# --- timedelta + range ------------------------------------------------------


@pytest.mark.parametrize("seconds", [3_600, 86_400, 604_800, 7_776_000])
def test_horizon_to_timedelta_round_trip(seconds: int) -> None:
    assert horizon_to_timedelta(seconds) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (HORIZON_MIN_SECONDS - 1, False),
        (HORIZON_MIN_SECONDS, True),
        (HORIZON_MAX_SECONDS, True),
        (HORIZON_MAX_SECONDS + 1, False),
        (10_800, True),  # "3 hours"
    ],
)
def test_is_in_range_boundary_semantics(seconds: int, expected: bool) -> None:
    assert is_in_range(seconds) is expected
