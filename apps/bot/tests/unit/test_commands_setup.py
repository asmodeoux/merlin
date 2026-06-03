"""Unit tests for the slash-menu registration.

We use a tiny fake ``Application`` exposing a ``bot.set_my_commands``
async method that records every invocation. No PTB internals, no
network. The ``Settings`` instances are constructed with
``_env_file=None`` so the developer's local ``apps/bot/.env`` doesn't
leak into the test run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from merlin_bot.commands_setup import (
    ADMIN_EXTRA_COMMANDS,
    PUBLIC_COMMANDS,
    register_commands,
)
from merlin_bot.config import Settings
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import BadRequest


def _settings(**kwargs: object) -> Settings:
    # ``arg-type`` covers BaseSettings rejecting ``**dict[str, object]``
    # against each declared field's narrow type. Runtime contract is
    # checked by the assertions below; static side stays quiet.
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


@dataclass
class _FakeBot:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: Any | None = None,
    ) -> bool:
        self.calls.append({"commands": list(commands), "scope": scope})
        return True


@dataclass
class _FakeApplication:
    bot: _FakeBot = field(default_factory=_FakeBot)


def test_public_commands_cover_every_user_facing_action() -> None:
    expected = {"start", "alpha", "me", "board", "tickers", "history", "help", "cancel"}
    actual = {cmd.command for cmd in PUBLIC_COMMANDS}
    assert actual == expected


def test_public_command_descriptions_fit_telegram_limit() -> None:
    """Telegram caps the description at 256 chars; most clients render
    only the first 40 or so. We keep ours short."""
    for cmd in PUBLIC_COMMANDS:
        assert 1 <= len(cmd.description) <= 80


def test_admin_extra_commands_includes_invite() -> None:
    """``/invite`` lands in Sprint 1 — admins mint invite codes via it."""
    cmds = {cmd.command for cmd in ADMIN_EXTRA_COMMANDS}
    assert "invite" in cmds
    for cmd in ADMIN_EXTRA_COMMANDS:
        assert 1 <= len(cmd.description) <= 80


async def test_register_commands_pushes_public_scope() -> None:
    app = _FakeApplication()
    await register_commands(app, settings=_settings(telegram_admin_chat_ids=""))  # type: ignore[arg-type]

    assert len(app.bot.calls) == 1
    call = app.bot.calls[0]
    assert isinstance(call["scope"], BotCommandScopeDefault)
    assert [cmd.command for cmd in call["commands"]] == [c.command for c in PUBLIC_COMMANDS]


async def test_register_commands_pushes_admin_scope_for_each_chat_id() -> None:
    app = _FakeApplication()
    await register_commands(
        app,  # type: ignore[arg-type]
        settings=_settings(telegram_admin_chat_ids="111,222"),
    )

    assert len(app.bot.calls) == 3
    assert isinstance(app.bot.calls[0]["scope"], BotCommandScopeDefault)

    admin_scopes = [c["scope"] for c in app.bot.calls[1:]]
    chat_ids = {s.chat_id for s in admin_scopes if isinstance(s, BotCommandScopeChat)}
    assert chat_ids == {111, 222}


async def test_register_commands_handles_single_admin_id() -> None:
    """The user's most common case: one admin id, no comma."""
    app = _FakeApplication()
    await register_commands(
        app,  # type: ignore[arg-type]
        settings=_settings(telegram_admin_chat_ids="987654321"),
    )

    assert len(app.bot.calls) == 2
    admin_call = app.bot.calls[1]
    assert isinstance(admin_call["scope"], BotCommandScopeChat)
    assert admin_call["scope"].chat_id == 987654321


# ---------------------------------------------------------------------------
# Regression for bug #6 — Telegram rejects ``set_my_commands`` for a
# ``BotCommandScopeChat`` if the chat doesn't exist server-side yet
# (i.e. the admin hasn't run ``/start`` yet). On a fresh deploy this
# error is GUARANTEED to fire on the very first boot. The bot must
# warn and continue, not crash.
# ---------------------------------------------------------------------------


@dataclass
class _ChatNotFoundBot:
    """Fake bot that succeeds on default scope, raises on chat scope.

    Mirrors Telegram's real first-boot behaviour: the public command
    menu pushes fine (no chat needed), but every per-chat push for an
    admin that hasn't ``/start``'d yet returns ``400 Chat not found``.
    """

    public_calls: int = 0
    # ``BotCommandScopeChat.chat_id`` is typed ``str | int`` upstream
    # (Telegram allows ``@channelname`` aliases). Our admin ids are
    # always plain ints, but the fixture's list type has to accept
    # the wider PTB shape or the static check breaks.
    chat_scope_attempts: list[int | str] = field(default_factory=list)

    async def set_my_commands(
        self,
        commands: list[BotCommand],
        scope: Any | None = None,
    ) -> bool:
        if isinstance(scope, BotCommandScopeChat):
            self.chat_scope_attempts.append(scope.chat_id)
            raise BadRequest("Chat not found")
        self.public_calls += 1
        return True


@dataclass
class _ChatNotFoundApplication:
    bot: _ChatNotFoundBot = field(default_factory=_ChatNotFoundBot)


async def test_register_commands_tolerates_badrequest_on_admin_chat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug #6 regression: a ``BadRequest`` from Telegram on the
    admin-scope push must NOT propagate. The public command menu has
    already been registered; the bot is otherwise functional, and the
    admin menu will land on the next restart after they ``/start``."""
    app = _ChatNotFoundApplication()

    with caplog.at_level(logging.WARNING, logger="merlin.bot.commands_setup"):
        # Must not raise.
        await register_commands(
            app,  # type: ignore[arg-type]
            settings=_settings(telegram_admin_chat_ids="151594111"),
        )

    assert app.bot.public_calls == 1, "public command push must succeed"
    assert app.bot.chat_scope_attempts == [
        151594111
    ], "we must still attempt the admin push so it lands on the next boot when the chat exists"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "the BadRequest must be logged as a warning"
    msg = warnings[0].getMessage()
    assert "151594111" in msg, "the warning must name the chat id that failed"
    assert "/start" in msg, "the warning must hint at the remediation"


async def test_register_commands_continues_to_other_admins_after_badrequest() -> None:
    """If one admin chat-id 400s, the others must still be tried.

    Otherwise a single mis-typed admin id would silently lock out
    every other admin's menu."""
    # ``str | int`` because ``BotCommandScopeChat.chat_id`` accepts
    # alias strings upstream (see fixture above).
    attempts: list[int | str] = []

    @dataclass
    class _PartialFailBot:
        async def set_my_commands(
            self,
            commands: list[BotCommand],
            scope: Any | None = None,
        ) -> bool:
            if isinstance(scope, BotCommandScopeChat):
                attempts.append(scope.chat_id)
                if scope.chat_id == 111:
                    raise BadRequest("Chat not found")
            return True

    @dataclass
    class _App:
        bot: _PartialFailBot = field(default_factory=_PartialFailBot)

    await register_commands(
        _App(),  # type: ignore[arg-type]
        settings=_settings(telegram_admin_chat_ids="111,222,333"),
    )
    assert attempts == [111, 222, 333], f"all three chat-ids must be tried; saw {attempts}"
