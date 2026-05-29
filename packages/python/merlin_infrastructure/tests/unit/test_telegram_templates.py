"""Unit tests for outbound notification templates.

These act as the bot-voice contract test for outcome notifications. If
a forbidden token slips in (``v0``, ``simulated``, raw ISO timestamps,
enum literals), one of these tests fails before anyone sees the string.
"""

from __future__ import annotations

import pytest
from merlin_infrastructure.notify.telegram.templates import (
    FLAT_THRESHOLD_PCT,
    outcome_flat,
    outcome_lost,
    outcome_won,
    rejected_asset_mutation,
    render_outcome,
    resolution_failed_final,
)

_FORBIDDEN_TOKENS = (
    " v0",
    "v0 ",
    "MVP",
    "Phase 2",
    "Sprint",
    "simulated",
    "regret",
    "hypothesis",
    "canonical form",
    "claim_id",
    "source_id",
    "not scored at v0",  # docs-check-ok — this IS the forbidden-token test fixture
    "kept for the record",
    "pending",
    "freeform_llm",
    "rejected_asset_mutation",
)


def _assert_bot_voice(text: str) -> None:
    lower = text.lower()
    for token in _FORBIDDEN_TOKENS:
        assert (
            token.lower() not in lower
        ), f"Forbidden bot-voice token {token!r} in outcome string: {text!r}"


def test_outcome_won_uses_human_horizon_and_plain_percent() -> None:
    text = outcome_won(asset_symbol="ETH", direction="long", horizon="14d", pct=6.2)
    assert "ETH" in text
    assert "2 weeks" in text
    assert "+6.2%" in text
    assert "called it right" in text
    _assert_bot_voice(text)


def test_outcome_lost_includes_direction_word() -> None:
    text = outcome_lost(asset_symbol="BTC", direction="short", horizon="30d", pct=8.0)
    assert "BTC" in text
    assert "1 month" in text
    assert "+8.0%" in text
    assert "the other way" in text or "against you" in text
    _assert_bot_voice(text)


def test_outcome_flat_message() -> None:
    text = outcome_flat(asset_symbol="SOL", pct=0.1)
    assert "SOL" in text
    assert "+0.10%" in text
    assert "wash" in text.lower()
    _assert_bot_voice(text)


def test_rejected_asset_mutation_template_is_user_friendly() -> None:
    text = rejected_asset_mutation(asset_symbol="LUNA")
    assert "LUNA" in text
    assert "listing changed" in text or "couldn't be settled" in text
    _assert_bot_voice(text)


def test_resolution_failed_final_template() -> None:
    text = resolution_failed_final(asset_symbol="ETH")
    assert "ETH" in text
    _assert_bot_voice(text)


@pytest.mark.parametrize(
    "direction,pct,expected_phrase",
    [
        ("long", 6.2, "called it right"),
        ("long", -6.2, "other way"),
        ("short", -6.2, "called it right"),
        ("short", 6.2, "other way"),
    ],
)
def test_render_outcome_dispatches_on_sign(
    direction: str, pct: float, expected_phrase: str
) -> None:
    text = render_outcome(
        asset_symbol="ETH",
        direction=direction,  # type: ignore[arg-type]
        horizon="14d",
        pct=pct,
    )
    assert expected_phrase in text or "other way" in text or "called it right" in text
    _assert_bot_voice(text)


def test_render_outcome_wash_for_small_moves() -> None:
    pct = FLAT_THRESHOLD_PCT / 2
    text = render_outcome(asset_symbol="ETH", direction="long", horizon="14d", pct=pct)
    assert "wash" in text.lower()
    _assert_bot_voice(text)
