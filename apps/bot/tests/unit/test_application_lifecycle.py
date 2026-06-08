"""Regression — ``build_application`` must inject runtime singletons
INSIDE ``post_init``, not before ``run_polling``.

The PTB lifecycle on ``Application.initialize()`` calls
``persistence._update_bot_data()`` which **overwrites**
``application.bot_data`` with whatever the persistence layer returns
(an empty dict on a fresh ``DictPersistence`` boot). So any
``application.bot_data[k] = v`` written between ``builder.build()`` and
``run_polling()`` gets clobbered before any handler ever sees it.

This bit us in Sprint 1: ``attach_client`` set the admin allowlist on
``bot_data``; the persistence overwrite emptied it; ``/start`` from the
admin account saw an empty ``BOT_DATA_ADMIN_KEY`` and fell through to
the public "Paste your invite code" prompt.

These tests pin the fix: client + admin allowlist must land via
``post_init``, surviving the persistence load.
"""

from __future__ import annotations

import pytest
from merlin_bot.api_dependency import BOT_DATA_ADMIN_KEY, BOT_DATA_KEY
from merlin_bot.application import build_application
from merlin_bot.config import Settings
from telegram.warnings import PTBUserWarning

# The /alpha ConversationHandler uses ``per_message=False`` (mixed text +
# CallbackQuery flow) and PTB emits a benign warning on build. We treat
# warnings as errors globally; allow this specific one in this file.
pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBUserWarning")

# Silence the unused-import — re-exported here so the type checker knows
# this is intentional.
_ = PTBUserWarning


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        telegram_bot_token="x" * 40,  # any non-empty value lets builder boot
        telegram_admin_chat_ids="151594111",  # type: ignore[arg-type]
        merlin_api_base_url="http://test-api",
        merlin_api_internal_token="test-token",
        sentry_dsn="",
    )


def test_bot_data_is_empty_before_post_init(settings: Settings) -> None:
    """Right after ``build_application`` returns, ``bot_data`` MUST be empty.

    Anything we'd write here gets overwritten by ``persistence._update_bot_data``
    on ``run_polling`` (PTB lifecycle). This test catches a regression
    where someone reintroduces the pre-Sprint-1 ``attach_client`` call
    in the wrong place.
    """
    app = build_application(settings)
    assert BOT_DATA_KEY not in app.bot_data, (
        f"{BOT_DATA_KEY!r} found in bot_data BEFORE post_init — this means "
        "the merlin_client was attached too early and will be clobbered "
        "by persistence on the next bot startup. Move the attach into "
        "``_post_init`` inside build_application."
    )
    assert BOT_DATA_ADMIN_KEY not in app.bot_data, (
        f"{BOT_DATA_ADMIN_KEY!r} found in bot_data BEFORE post_init — same "
        "problem as above; the admin allowlist must land in post_init."
    )


async def test_post_init_populates_bot_data(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``post_init`` runs, ``bot_data`` has the runtime singletons.

    The bug we ate in Sprint 1 was that admin ``/start`` saw an empty
    allowlist and fell through to the public flow. This test verifies
    post_init actually populates it.

    ``register_commands`` is stubbed because it makes a live Telegram
    API call; we're testing the bot_data wiring contract, not the
    command-menu push (which has its own dedicated tests).
    """

    async def _noop(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr("merlin_bot.application.register_commands", _noop)

    app = build_application(settings)

    post_init = app.post_init
    assert post_init is not None, (
        "build_application must register a post_init hook so the "
        "merlin_client + admin allowlist land AFTER persistence load."
    )

    await post_init(app)

    assert BOT_DATA_KEY in app.bot_data, (
        f"{BOT_DATA_KEY!r} missing after post_init — handlers can't reach the MerlinClient."
    )
    assert BOT_DATA_ADMIN_KEY in app.bot_data, (
        f"{BOT_DATA_ADMIN_KEY!r} missing after post_init — admin "
        "/start will fall through to the public invite flow."
    )
    assert 151594111 in app.bot_data[BOT_DATA_ADMIN_KEY], (
        "configured admin chat id missing from the allowlist set."
    )


async def test_post_init_survives_persistence_overwrite(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic simulation: PTB clears bot_data on persistence
    load, then runs post_init. After post_init, bot_data must be
    populated.

    If a future refactor moves ``attach_client`` back outside
    ``post_init``, this test fails — the manual clear here mimics what
    ``Application.initialize()`` actually does.
    """

    async def _noop(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr("merlin_bot.application.register_commands", _noop)

    app = build_application(settings)

    # Mimic what PTB's Application.initialize() does at startup.
    app.bot_data.clear()

    post_init = app.post_init
    assert post_init is not None
    await post_init(app)

    assert app.bot_data.get(BOT_DATA_ADMIN_KEY) == frozenset({151594111})
    assert BOT_DATA_KEY in app.bot_data
