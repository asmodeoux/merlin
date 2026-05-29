"""Unit tests for the env-check script.

Focused on the two failure modes the CI gate enforces:

1. Settings field <-> .env.example key drift.
2. Quote-wrapped secret values in a local apps/*/.env (a real bug we hit
   during the first Sprint-1 dev-stack bring-up).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from env_check import _find_quoted_values, _read_env_example_keys


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def test_read_env_example_keys_skips_comments_and_blanks(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        ".env.example",
        """# header comment
ONE=value
# another comment

TWO=
THREE=three
""",
    )
    assert _read_env_example_keys(env) == {"ONE", "TWO", "THREE"}


def test_find_quoted_values_detects_double_quotes(tmp_path: Path) -> None:
    env = _write(tmp_path, ".env", 'OPENROUTER_API_KEY="sk-or-v1-abc"\n')
    assert _find_quoted_values(env) == ["OPENROUTER_API_KEY"]


def test_find_quoted_values_detects_single_quotes(tmp_path: Path) -> None:
    env = _write(tmp_path, ".env", "TELEGRAM_BOT_TOKEN='1234:abc'\n")
    assert _find_quoted_values(env) == ["TELEGRAM_BOT_TOKEN"]


def test_find_quoted_values_ignores_unquoted_values(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        ".env",
        "TELEGRAM_BOT_TOKEN=1234:abc\nOPENROUTER_API_KEY=sk-or-v1-abc\n",
    )
    assert _find_quoted_values(env) == []


def test_find_quoted_values_ignores_blank_values(tmp_path: Path) -> None:
    """``KEY=`` is fine — it just disables that secret. Don't flag it."""
    env = _write(tmp_path, ".env", "OPENROUTER_API_KEY=\n")
    assert _find_quoted_values(env) == []


def test_find_quoted_values_ignores_mismatched_quotes(tmp_path: Path) -> None:
    """``KEY="abc'`` is malformed in another way — leave it to the
    secret-consuming code to fail loudly; we only catch the
    *symmetric* quote-wrapping case which is what humans actually do."""
    env = _write(tmp_path, ".env", "TELEGRAM_BOT_TOKEN=\"abc'\n")
    assert _find_quoted_values(env) == []


def test_find_quoted_values_flags_multiple_keys(tmp_path: Path) -> None:
    env = _write(
        tmp_path,
        ".env",
        """OPENROUTER_API_KEY="sk-or-v1-abc"
TELEGRAM_BOT_TOKEN="1234:abc"
COINGECKO_API_KEY=
PLAIN_VALUE=plain
""",
    )
    assert _find_quoted_values(env) == ["OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN"]


@pytest.mark.parametrize(
    "line,expected",
    [
        ('OPENROUTER_API_KEY="sk-or-v1-abc"\n', ["OPENROUTER_API_KEY"]),
        ("OPENROUTER_API_KEY=sk-or-v1-abc\n", []),
        ('LLM_MONTHLY_USD_BUDGET="30.0"\n', ["LLM_MONTHLY_USD_BUDGET"]),
        ("LLM_MONTHLY_USD_BUDGET=30.0\n", []),
    ],
)
def test_find_quoted_values_parametrized(tmp_path: Path, line: str, expected: list[str]) -> None:
    env = _write(tmp_path, ".env", line)
    assert _find_quoted_values(env) == expected
