"""Every app's ``Settings`` must parse from its own ``.env.example``.

Regression suite for bug #5 (Sprint 1 Docker bring-up): the worker's
``RQ_QUEUES`` env var was typed ``list[str]``, which pydantic-settings
defaults to JSON-decoding. The ``.env.example`` shipped with a
comma-separated value (``default,resolution,reputation``) — the only
sane shape for a shell env-var — and the worker crashed at startup
with a ``JSONDecodeError``. No test ever ran ``Settings()`` against
the real ``.env.example`` because every unit test hand-built
``Settings(...)`` with kwargs.

Same family of trap exists across every app — anywhere a ``list[T]``,
``dict[...]``, or other complex type is exposed via the env, a CSV /
``key=value`` shape in the example file would crash on load.

These tests pin the contract: **if the ``.env.example`` exists and
documents a value, the matching ``Settings`` must be able to load it.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(app: str) -> Path:
    return _REPO_ROOT / "apps" / app / ".env.example"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_settings_parses_env_example() -> None:
    """``apps/api/.env.example`` must produce a valid ``Settings``."""
    from merlin_api.config import Settings

    s = Settings(_env_file=str(_env_path("api")))
    # Sanity-check a couple of fields land where expected. Use ``or``
    # so an empty placeholder still passes — what we're guarding
    # against is a *parse* crash, not blank values.
    assert s.environment in {"dev", "staging", "prod", "test", ""} or s.environment
    assert isinstance(s.llm_monthly_usd_budget, float)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


def test_bot_settings_parses_env_example() -> None:
    """``apps/bot/.env.example`` must produce a valid ``Settings``."""
    from merlin_bot.config import Settings

    s = Settings(_env_file=str(_env_path("bot")))
    # Same defence: confirm the parse worked, not that values are non-empty.
    assert isinstance(s.telegram_admin_chat_ids, list)


def test_bot_settings_parses_admin_chat_ids_as_csv() -> None:
    """Regression: ``TELEGRAM_ADMIN_CHAT_IDS`` must accept ``123,456`` —
    pydantic-settings' default ``list[int]`` would demand JSON."""
    from merlin_bot.config import Settings

    s = Settings(
        _env_file=None,
        telegram_bot_token="x" * 16,
        telegram_admin_chat_ids="123,456,789",  # type: ignore[arg-type]
    )
    assert s.telegram_admin_chat_ids == [123, 456, 789]


def test_bot_settings_parses_admin_chat_ids_single_int() -> None:
    """A bare ``123`` (no commas) should still produce a single-element list."""
    from merlin_bot.config import Settings

    s = Settings(
        _env_file=None,
        telegram_bot_token="x" * 16,
        telegram_admin_chat_ids="42",  # type: ignore[arg-type]
    )
    assert s.telegram_admin_chat_ids == [42]


def test_bot_settings_parses_admin_chat_ids_empty() -> None:
    """An empty string env value should yield an empty list, not crash."""
    from merlin_bot.config import Settings

    s = Settings(
        _env_file=None,
        telegram_bot_token="x" * 16,
        telegram_admin_chat_ids="",  # type: ignore[arg-type]
    )
    assert s.telegram_admin_chat_ids == []


# ---------------------------------------------------------------------------
# Worker — the actual bug #5 site
# ---------------------------------------------------------------------------


def test_worker_settings_parses_env_example() -> None:
    """``apps/worker/.env.example`` must produce a valid ``Settings``.

    This is the exact regression: ``RQ_QUEUES`` was documented as
    ``default,resolution,reputation`` in the example file but typed as
    ``list[str]`` on the Settings class. pydantic-settings JSON-decodes
    list types by default; the result was a startup crash on the very
    first boot."""
    from merlin_worker.config import Settings

    s = Settings(_env_file=str(_env_path("worker")))
    assert isinstance(s.rq_queues, list)
    assert all(isinstance(q, str) for q in s.rq_queues)
    assert len(s.rq_queues) >= 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("default,resolution,reputation", ["default", "resolution", "reputation"]),
        ("default, resolution , reputation", ["default", "resolution", "reputation"]),
        ("default", ["default"]),
        ("", ["default", "resolution", "reputation"]),  # empty → factory default
    ],
)
def test_worker_settings_parses_rq_queues_as_csv(raw: str, expected: list[str]) -> None:
    """The validator must accept every form a shell user might type."""
    from merlin_worker.config import Settings

    s = Settings(_env_file=None, rq_queues=raw)  # type: ignore[arg-type]
    assert s.rq_queues == expected


def test_worker_settings_parses_rq_queues_as_list_passthrough() -> None:
    """An already-typed list (the in-process kwargs path) must pass through."""
    from merlin_worker.config import Settings

    s = Settings(_env_file=None, rq_queues=["a", "b"])
    assert s.rq_queues == ["a", "b"]
