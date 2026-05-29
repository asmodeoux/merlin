"""Unit tests for the preview-rendering helpers.

These are the pure functions that compose the user-facing preview /
receipt strings. PTB-bound coroutines (``show_preview``, ``on_confirm``)
are exercised by the integration suite — here we only assert the bot-
voice contract for the string output.
"""

from __future__ import annotations

import pytest
from merlin_bot.handlers.alpha import step_preview as sp


def test_render_preview_full_draft() -> None:
    text = sp._render_preview(
        {
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
        },
        {"symbol": "ETH", "coingecko_id": "ethereum"},
    )
    # One field per line — pin the contract so an accidental revert
    # back to the "ETH · long · 2 weeks · strong" inline summary is
    # caught by the test rather than by a confused user.
    assert "Asset: ETH" in text
    assert "Direction: Long" in text
    assert "Horizon: 2 weeks" in text
    assert "Conviction: Strong" in text
    assert "Vitalik rumour" in text
    # Footer should invite Confirm only when the draft is complete.
    assert "Confirm" in text


def test_render_preview_uses_placeholders_when_blank() -> None:
    text = sp._render_preview({}, {})
    # Placeholders match the title-cased Edit button labels so the
    # user's eye can jump from blank line to matching button.
    assert "Asset: (tap ✏️ Edit Asset)" in text
    assert "Direction: (tap ✏️ Edit Direction)" in text
    assert "Horizon: (tap ✏️ Edit Horizon)" in text
    assert "Conviction: (tap ✏️ Edit Conviction)" in text
    # Footer must NOT invite Confirm when fields are missing.
    assert "Confirm" not in text
    assert "Fill in the missing fields" in text


def test_render_receipt_includes_all_fields_and_utc_timestamps() -> None:
    """Receipt must echo what was saved + full UTC date/time so the
    user gets unambiguous confirmation and knows exactly when the bot
    will check the call. Sprint-1 receipt was a one-liner that didn't
    show the bet — "did it actually save?" was a real question."""
    text = sp._render_receipt(
        result={
            "asset_symbol": "ETH",
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "submitted_at": "2026-05-29T16:32:00Z",
            "resolves_at": "2026-06-12T16:32:00Z",
        },
        draft={
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
        },
        asset={"symbol": "ETH", "coingecko_id": "ethereum"},
    )
    # Banner + every field echoed.
    assert "Saved" in text
    assert "Asset: ETH" in text
    assert "Direction: Long" in text
    assert "Horizon: 2 weeks" in text
    assert "Conviction: Strong" in text
    assert "Vitalik rumour" in text
    # Full UTC date+time (not just "Jun 12", not just "in 2 weeks").
    assert "2026-05-29 16:32 UTC" in text
    assert "2026-06-12 16:32 UTC" in text
    # Pointer to where the user can see open calls.
    assert "/me" in text


def test_utc_datetime_normalises_offset_timestamps() -> None:
    """ISO strings with an explicit offset (``+02:00``, etc.) must be
    converted to UTC, not displayed verbatim — otherwise different
    servers in different TZs would print different times."""
    text = sp._utc_datetime("2026-06-12T18:32:00+02:00")
    assert text == "2026-06-12 16:32 UTC"


def test_utc_datetime_assumes_utc_on_naive_input() -> None:
    text = sp._utc_datetime("2026-06-12T16:32:00")
    assert text == "2026-06-12 16:32 UTC"


def test_utc_datetime_handles_z_suffix() -> None:
    text = sp._utc_datetime("2026-06-12T16:32:00Z")
    assert text == "2026-06-12 16:32 UTC"


def test_utc_datetime_returns_soon_for_garbage_input() -> None:
    assert sp._utc_datetime("") == "soon"
    assert sp._utc_datetime("not-a-date") == "soon"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("1h", "1 hour"),
        ("24h", "24 hours"),
        ("7d", "1 week"),
        ("14d", "2 weeks"),
        ("30d", "1 month"),
        ("90d", "3 months"),
    ],
)
def test_horizon_human_maps_codes(code: str, expected: str) -> None:
    assert sp._horizon_human(code) == expected


def test_horizon_human_unknown_returns_placeholder() -> None:
    assert sp._horizon_human(None) == "(tap ✏️ Edit Horizon)"
    assert sp._horizon_human("99y") == "(tap ✏️ Edit Horizon)"


@pytest.mark.parametrize("code,expected", [("long", "Long"), ("short", "Short")])
def test_direction_human(code: str, expected: str) -> None:
    assert sp._direction_human(code) == expected


def test_direction_human_unknown_returns_placeholder() -> None:
    assert sp._direction_human(None) == "(tap ✏️ Edit Direction)"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("weak", "Weak"),
        ("medium", "Medium"),
        ("strong", "Strong"),
        ("very_strong", "Very strong"),
    ],
)
def test_conviction_human(code: str, expected: str) -> None:
    assert sp._conviction_human(code) == expected


def test_build_submit_payload_with_full_draft() -> None:
    payload = sp._build_submit_payload(
        {
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "extracted_fields": {"schema_version": 1, "rumour_referenced": True},
            "parse_metadata": {"parsed_via": "freeform_llm"},
        },
        {"coingecko_id": "ethereum", "symbol": "ETH"},
    )
    assert payload is not None
    assert payload["coingecko_id"] == "ethereum"
    assert payload["direction"] == "long"
    assert payload["extracted_fields"]["rumour_referenced"] is True


def test_build_submit_payload_missing_required_field_returns_none() -> None:
    payload = sp._build_submit_payload(
        {"direction": "long", "horizon": "14d"},  # no conviction or thesis
        {"coingecko_id": "ethereum"},
    )
    assert payload is None


def test_build_submit_payload_no_asset_returns_none() -> None:
    payload = sp._build_submit_payload(
        {
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "ok",
        },
        {},
    )
    assert payload is None


def test_calendar_date_renders_short_form() -> None:
    text = sp._calendar_date("2026-06-12T09:34:00Z")
    assert "Jun" in text


def test_calendar_date_fallback_when_unparseable() -> None:
    assert sp._calendar_date("") == "soon"
    assert sp._calendar_date("not-a-date") == "soon"


# ---------------------------------------------------------------------------
# Keyboard contract
# ---------------------------------------------------------------------------


def _button_labels(kb: object) -> list[str]:
    """Flatten an InlineKeyboardMarkup into a list of button text labels."""
    labels: list[str] = []
    for row in kb.inline_keyboard:  # type: ignore[attr-defined]
        for btn in row:
            labels.append(btn.text)
    return labels


_FULL_DRAFT = {
    "direction": "long",
    "horizon": "14d",
    "conviction": "strong",
    "thesis_text": "Vitalik rumour",
}
_FULL_ASSET = {"coingecko_id": "ethereum", "symbol": "ETH"}


def test_keyboard_shows_confirm_when_draft_is_complete() -> None:
    kb = sp._build_preview_keyboard(_FULL_DRAFT, _FULL_ASSET)
    labels = _button_labels(kb)
    assert "✅ Confirm" in labels


@pytest.mark.parametrize(
    "missing_field",
    ["direction", "horizon", "conviction", "thesis_text"],
)
def test_keyboard_hides_confirm_when_draft_field_missing(missing_field: str) -> None:
    """User MUST NOT be able to confirm an incomplete bet — the
    Confirm button does not render until every required field is set."""
    draft = dict(_FULL_DRAFT)
    draft[missing_field] = "" if missing_field == "thesis_text" else None
    kb = sp._build_preview_keyboard(draft, _FULL_ASSET)
    labels = _button_labels(kb)
    assert "✅ Confirm" not in labels, (
        f"Confirm button rendered with {missing_field}=blank. "
        "Hidden-until-complete is the rule; never the validate-after-click path."
    )


def test_keyboard_hides_confirm_when_asset_missing() -> None:
    kb = sp._build_preview_keyboard(_FULL_DRAFT, {})
    labels = _button_labels(kb)
    assert "✅ Confirm" not in labels


def test_keyboard_always_shows_edit_and_cancel() -> None:
    """Edit + Cancel must remain reachable even when the draft is
    incomplete — otherwise the user has no way to recover."""
    kb = sp._build_preview_keyboard({}, {})
    labels = _button_labels(kb)
    for expected in [
        "✏️ Edit Asset",
        "✏️ Edit Direction",
        "✏️ Edit Horizon",
        "✏️ Edit Conviction",
        "✏️ Edit Thesis",
        "❌ Cancel",
    ]:
        assert expected in labels, f"missing button: {expected!r} in {labels!r}"


def test_is_draft_complete_mirrors_submit_payload_preconditions() -> None:
    """``_is_draft_complete`` is the keyboard's gate; ``_build_submit_payload``
    is the on-confirm validator. They MUST agree — if the keyboard
    shows Confirm, the submit MUST succeed; if hidden, MUST fail."""
    # Complete → both pass.
    assert sp._is_draft_complete(_FULL_DRAFT, _FULL_ASSET) is True
    assert sp._build_submit_payload(_FULL_DRAFT, _FULL_ASSET) is not None
    # Missing asset → both refuse.
    assert sp._is_draft_complete(_FULL_DRAFT, {}) is False
    assert sp._build_submit_payload(_FULL_DRAFT, {}) is None
    # Missing horizon → both refuse.
    bad = dict(_FULL_DRAFT, horizon=None)
    assert sp._is_draft_complete(bad, _FULL_ASSET) is False
    assert sp._build_submit_payload(bad, _FULL_ASSET) is None
