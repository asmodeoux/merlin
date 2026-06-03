"""Unit tests for outbound notification templates.

These act as the bot-voice contract test for outcome notifications. If
a forbidden token slips in (``v0``, ``simulated``, raw ISO timestamps,
enum literals), one of these tests fails before anyone sees the string.
"""

from __future__ import annotations

import pytest
from merlin_infrastructure.notify.telegram.templates import (
    FLAT_THRESHOLD_PCT,
    invitee_joined,
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
    text = outcome_won(asset_symbol="ETH", direction="long", horizon_seconds=1_209_600, pct=6.2)
    assert "ETH" in text
    assert "2 weeks" in text
    assert "+6.2%" in text
    assert "called it right" in text
    _assert_bot_voice(text)


def test_outcome_lost_includes_direction_word() -> None:
    text = outcome_lost(asset_symbol="BTC", direction="short", horizon_seconds=2_592_000, pct=8.0)
    assert "BTC" in text
    # 30 days is rendered as "30 days" (ambiguous "this month" vs "30
    # days exactly"; humanise_horizon keeps it explicit). The MVP
    # template promise — "no raw codes, plain calendar phrase" — is
    # what we assert here, not a specific synonym.
    assert "30 days" in text
    assert "+8.0%" in text
    assert "the other way" in text or "against you" in text
    _assert_bot_voice(text)


def test_outcome_won_renders_sub_day_horizon_in_hours() -> None:
    """Pin the bot-voice rendering for hour-scale calls (3h / 4h /
    12h) — the gap the continuous-horizon migration was meant to
    fix. A "3 hours" call should resolve into a message that says
    "3 hours", not "0 days" or "0.125 days" or "10800 seconds"."""
    text = outcome_won(asset_symbol="BTC", direction="long", horizon_seconds=10_800, pct=2.1)
    assert "3 hours" in text
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
        horizon_seconds=1_209_600,
        pct=pct,
    )
    assert expected_phrase in text or "other way" in text or "called it right" in text
    _assert_bot_voice(text)


def test_render_outcome_wash_for_small_moves() -> None:
    pct = FLAT_THRESHOLD_PCT / 2
    text = render_outcome(
        asset_symbol="ETH",
        direction="long",
        horizon_seconds=1_209_600,
        pct=pct,
    )
    assert "wash" in text.lower()
    _assert_bot_voice(text)


# ---------------------------------------------------------------------------
# Updated PnL chunk + /me · /board CTA — added to won/lost/flat templates
# so the user sees their post-resolution score and a route back to /me +
# /board on every outcome notification. Pinning the surface here keeps the
# resolver path and the read path's "PnL" label in lockstep.
# ---------------------------------------------------------------------------


def test_outcome_won_includes_updated_pnl_chunk_when_provided() -> None:
    """Resolver-supplied PnL renders as ``Your PnL is now +X.X%.``

    Sign is preserved with a leading + or -, decimal places match
    the /board + /me renders (1 d.p.), and the chunk lands BEFORE
    the /me · /board CTA so the natural reading is "here's your
    new number — go see the rest".
    """
    text = outcome_won(
        asset_symbol="TON",
        direction="short",
        horizon_seconds=86_400,
        pct=-7.6,
        weighted_pnl_pct=6.1,
    )
    assert "Your PnL is now +6.1%." in text
    assert "/me" in text
    assert "/board" in text
    # CTA must follow the PnL line.
    assert text.index("Your PnL is now") < text.index("/me")
    _assert_bot_voice(text)


def test_outcome_won_separates_body_from_pnl_with_paragraph_break() -> None:
    """The PnL chunk + /me · /board CTA render on their own paragraph.

    Pins ``\\n\\n`` between the resolution body and the PnL tail so a
    future template tweak can't silently glue the two paragraphs back
    together. Telegram renders ``\\n`` as a soft wrap and ``\\n\\n``
    as a paragraph gap — we want the gap.
    """
    text = outcome_won(
        asset_symbol="TON",
        direction="short",
        horizon_seconds=86_400,
        pct=-7.6,
        weighted_pnl_pct=6.1,
    )
    # The transition from the resolution sentence to the PnL block is
    # exactly one paragraph break.
    assert "you called it right!\n\nYour PnL is now" in text
    # And there's no triple-newline (that would render as an extra
    # blank line that nobody asked for).
    assert "\n\n\n" not in text


def test_outcome_won_paragraph_break_still_renders_when_pnl_is_none() -> None:
    """The paragraph break holds even when the PnL chunk drops out.

    Inline-recompute failure (or cold-start edge) yields
    ``weighted_pnl_pct=None``. The body still flows into the CTA on
    a separate paragraph — the visual rhythm "what happened" /
    "what to do next" is preserved regardless of whether the PnL
    chunk is present.
    """
    text = outcome_won(
        asset_symbol="TON",
        direction="short",
        horizon_seconds=86_400,
        pct=-7.6,
        weighted_pnl_pct=None,
    )
    assert "you called it right!\n\nTap /me or /board" in text
    assert "Your PnL is now" not in text


def test_outcome_won_omits_pnl_chunk_when_recompute_returned_none() -> None:
    """A None PnL (inline recompute failed / cold-start edge) MUST drop
    the PnL sentence cleanly — no "Your PnL is now None" leaks — but
    the CTA still renders so the user has a path back to /me + /board.
    """
    text = outcome_won(
        asset_symbol="TON",
        direction="short",
        horizon_seconds=86_400,
        pct=-7.6,
        weighted_pnl_pct=None,
    )
    assert "None" not in text
    assert "Your PnL is now" not in text
    assert "/me" in text
    assert "/board" in text


def test_outcome_lost_includes_updated_pnl_chunk_when_provided() -> None:
    """Even a losing call updates the leaderboard PnL; the lost
    template must surface the new value so the reader doesn't have
    to /me + /board to find out the magnitude of the damage."""
    text = outcome_lost(
        asset_symbol="BTC",
        direction="long",
        horizon_seconds=7_200,
        pct=-1.32,
        weighted_pnl_pct=-1.3,
    )
    assert "-1.3%" in text  # the updated PnL (negative renders with -)
    assert "Your PnL is now -1.3%." in text
    assert "/me" in text
    assert "/board" in text
    _assert_bot_voice(text)


def test_outcome_flat_includes_updated_pnl_chunk_when_provided() -> None:
    """Flats contribute their honest signed value to PnL (honest_signed),
    so the flat template surfaces the new PnL too. The +0.10% body and
    the +0.3% updated-PnL chunk render side-by-side without colliding.
    """
    text = outcome_flat(asset_symbol="SOL", pct=0.1, weighted_pnl_pct=0.3)
    assert "+0.10%" in text  # the resolved-pct body
    assert "Your PnL is now +0.3%." in text  # the updated source PnL
    assert "wash" in text.lower()
    assert "/me" in text
    assert "/board" in text
    _assert_bot_voice(text)


def test_outcome_won_yuri_live_scenario_text_is_the_target_string() -> None:
    """End-to-end string assertion matching the user-requested wording.

    The exact live message we're targeting (per the chat conversation
    that motivated this change): a paragraph for the resolution body,
    then a paragraph break, then the PnL + /me · /board CTA.

        🎉 Your TON call resolved. Short over 24 hours ended -7.6%
        — you called it right!

        Your PnL is now +6.1%. Tap /me or /board for the rest.
    """
    text = outcome_won(
        asset_symbol="TON",
        direction="short",
        horizon_seconds=86_400,
        pct=-7.6,
        weighted_pnl_pct=6.125,
    )
    assert text == (
        "🎉 Your TON call resolved. Short over 24 hours ended -7.6% "
        "— you called it right!\n\n"
        "Your PnL is now +6.1%. Tap /me or /board for the rest."
    )


def test_render_outcome_threads_pnl_through_to_all_three_templates() -> None:
    """``render_outcome`` is the single entry point the resolver uses;
    pinning that it forwards ``weighted_pnl_pct`` to whichever sub-
    template fires catches the easy refactor mistake of "I added the
    param to outcome_won but not to render_outcome".
    """
    won = render_outcome(
        asset_symbol="ETH",
        direction="long",
        horizon_seconds=1_209_600,
        pct=6.2,
        weighted_pnl_pct=4.4,
    )
    lost = render_outcome(
        asset_symbol="ETH",
        direction="long",
        horizon_seconds=1_209_600,
        pct=-6.2,
        weighted_pnl_pct=-2.1,
    )
    flat = render_outcome(
        asset_symbol="ETH",
        direction="long",
        horizon_seconds=1_209_600,
        pct=FLAT_THRESHOLD_PCT / 2,
        weighted_pnl_pct=0.0,
    )

    assert "Your PnL is now +4.4%." in won
    assert "Your PnL is now -2.1%." in lost
    assert "Your PnL is now +0.0%." in flat
    for text in (won, lost, flat):
        assert "/me" in text
        assert "/board" in text


def test_rejected_and_failure_templates_do_not_render_pnl_chunk() -> None:
    """Q-023 reject + final-failure paths leave the leaderboard untouched
    (the source's reputation isn't recomputed for them), so they MUST
    NOT pretend to surface an updated PnL — the user would interpret
    the absence of a value as "your standing is unchanged". The
    rejection / failure templates own their own wording.
    """
    rej = rejected_asset_mutation(asset_symbol="LUNA")
    fail = resolution_failed_final(asset_symbol="ETH")

    for text in (rej, fail):
        assert "Your PnL is now" not in text
        assert "/me" not in text
        assert "/board" not in text


# ---------------------------------------------------------------------------
# invitee_joined — fires on the inviter side when their invitee finishes
# onboarding (first display-name set via PATCH /me).
# ---------------------------------------------------------------------------


def test_invitee_joined_renders_name_with_friendly_phrasing() -> None:
    """The string must address the inviter directly ("your invite code")
    and echo the invitee's display name verbatim. The handshake emoji
    leads so the inviter sees a positive social signal even in the
    chat-list preview before they open the message.
    """
    text = invitee_joined(name="Bob")
    assert text == "🤝🏻 Bob has joined using your invite code!"
    assert "your invite code" in text
    _assert_bot_voice(text)


def test_invitee_joined_passes_through_unicode_names() -> None:
    """Display names are user-controlled UTF-8 (Telegram allows any
    BMP + supplementary codepoints). The template MUST NOT mangle
    non-ASCII names — the inviter wouldn't recognise a mojibake'd
    handle as the person they invited.
    """
    text = invitee_joined(name="Юрий")
    assert "Юрий" in text
    _assert_bot_voice(text)
