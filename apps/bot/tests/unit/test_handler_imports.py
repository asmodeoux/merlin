"""Smoke imports for every bot handler module.

Catches broken imports / missing fixtures before the PTB integration
harness has to spin up. Cheap, no I/O.
"""

from __future__ import annotations


def test_start_handler_module_imports() -> None:
    from merlin_bot.handlers import start

    assert hasattr(start, "build_handler")
    assert hasattr(start, "start_command")
    assert hasattr(start, "receive_invite")
    assert hasattr(start, "receive_display_name")


def test_alpha_conversation_module_imports() -> None:
    from merlin_bot.handlers.alpha import conversation

    assert hasattr(conversation, "build_handler")


def test_alpha_step_modules_import() -> None:
    from merlin_bot.handlers.alpha import (
        step_asset,
        step_conviction,
        step_direction,
        step_freeform,
        step_horizon,
        step_preview,
        step_thesis,
    )

    for module in (
        step_asset,
        step_conviction,
        step_direction,
        step_freeform,
        step_horizon,
        step_preview,
        step_thesis,
    ):
        assert module is not None


def test_alpha_state_enum_members() -> None:
    from merlin_bot.handlers.alpha.states import AlphaState

    assert AlphaState.AWAIT_TEXT.value == 1
    assert AlphaState.PREVIEW.value == 2
    assert AlphaState.EDIT_DIRECTION.value < AlphaState.EDIT_THESIS.value


def test_alpha_step_callback_patterns_match() -> None:
    """Confirm/edit/cancel patterns are unique short strings (TG 64-byte cap)."""
    from merlin_bot.handlers.alpha import step_preview as sp

    patterns = {
        sp.CB_CONFIRM,
        sp.CB_CANCEL,
        sp.CB_EDIT_ASSET,
        sp.CB_EDIT_DIRECTION,
        sp.CB_EDIT_HORIZON,
        sp.CB_EDIT_CONVICTION,
        sp.CB_EDIT_THESIS,
    }
    assert len(patterns) == 7
    for p in patterns:
        assert p.startswith("alpha:")
        assert len(p) < 64


def test_start_state_enum_members() -> None:
    from merlin_bot.handlers.start import StartState

    assert StartState.AWAIT_INVITE.value == 1
    assert StartState.AWAIT_DISPLAY_NAME.value == 2
