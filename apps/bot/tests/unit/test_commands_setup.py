"""Unit tests for the slash-menu registration.

We use a tiny fake ``Application`` exposing a ``bot.set_my_commands``
async method that records every invocation. No PTB internals, no
network. The ``Settings`` instances are constructed with
``_env_file=None`` so the developer's local ``apps/bot/.env`` doesn't
leak into the test run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from merlin_bot.commands_setup import (
    ADMIN_EXTRA_COMMANDS,
    PUBLIC_COMMANDS,
    register_commands,
)
from merlin_bot.config import Settings
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


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
    expected = {"start", "alpha", "me", "board", "help", "cancel"}
    actual = {cmd.command for cmd in PUBLIC_COMMANDS}
    assert actual == expected


def test_public_command_descriptions_fit_telegram_limit() -> None:
    """Telegram caps the description at 256 chars; most clients render
    only the first 40 or so. We keep ours short."""
    for cmd in PUBLIC_COMMANDS:
        assert 1 <= len(cmd.description) <= 80


def test_admin_extra_commands_is_empty_at_sprint_1() -> None:
    """Operator commands land in Sprint 2; the slot is reserved."""
    assert ADMIN_EXTRA_COMMANDS == ()


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
