"""Tests for MarkdownV2 escaping helper."""

from __future__ import annotations

import pytest
from merlin_bot.formatting import MARKDOWN_V2_RESERVED, escape_md_v2


@pytest.mark.fast
def test_escape_passes_plain_text() -> None:
    """Letters and digits are passed through unchanged."""
    assert escape_md_v2("hello world 42") == "hello world 42"


@pytest.mark.fast
def test_escape_backslashes_every_reserved_char() -> None:
    """Each reserved character gets a leading backslash."""
    sample = "a_b*c[d]e(f)g~h`i>j#k+l-m=n|o{p}q.r!s\\"
    escaped = escape_md_v2(sample)
    for char in sample:
        if char in MARKDOWN_V2_RESERVED:
            assert f"\\{char}" in escaped


@pytest.mark.fast
def test_escape_handles_empty_string() -> None:
    assert escape_md_v2("") == ""
