"""Tests for merlin_core.domain.source_id — MVP §15 invariant 9."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from merlin_core.domain.source_id import HASH_LEN, TG_PREFIX, is_source_id, make_source_id


@pytest.mark.fast
def test_make_source_id_prefixed() -> None:
    """source_id is always prefixed with the TG channel marker."""
    sid = make_source_id(12345)
    assert sid.startswith(TG_PREFIX)


@pytest.mark.fast
def test_make_source_id_deterministic() -> None:
    """Same input → same source_id (we look users up by it)."""
    assert make_source_id(12345) == make_source_id(12345)
    assert make_source_id("alice") == make_source_id("alice")


@pytest.mark.fast
def test_make_source_id_int_str_equivalence() -> None:
    """int and str of the same value produce the same source_id."""
    assert make_source_id(12345) == make_source_id("12345")


@pytest.mark.fast
def test_make_source_id_distinct() -> None:
    """Different inputs produce different source_ids (collision-resistant)."""
    assert make_source_id(12345) != make_source_id(12346)


@pytest.mark.fast
def test_make_source_id_rejects_empty() -> None:
    """Empty / whitespace input is a programming error, not a silent bug."""
    with pytest.raises(ValueError):
        make_source_id("")
    with pytest.raises(ValueError):
        make_source_id("   ")


@pytest.mark.fast
def test_is_source_id_round_trip() -> None:
    """Anything make_source_id produces is_source_id accepts."""
    assert is_source_id(make_source_id(42))


@pytest.mark.fast
def test_is_source_id_rejects_raw() -> None:
    """Raw IDs and obvious nonsense don't pass the check."""
    assert not is_source_id("12345")
    assert not is_source_id("tg:short")
    assert not is_source_id("")


@given(st.one_of(st.integers(min_value=1), st.text(min_size=1, max_size=64).filter(str.strip)))
def test_make_source_id_property(value: int | str) -> None:
    """For any non-empty input, make_source_id returns a valid source_id."""
    sid = make_source_id(value)
    assert is_source_id(sid)
    assert len(sid) == len(TG_PREFIX) + HASH_LEN
