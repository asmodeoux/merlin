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
            "horizon_seconds": 1_209_600,
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


def test_render_preview_subhour_horizon_uses_hours_word() -> None:
    """The gap the continuous-horizon migration was meant to close —
    a "3 hours" call from "/alpha BTC long 3 hours" must render as
    "3 hours" in the preview, not "10800 seconds" / "0 days" / a
    nearest-bucket "4 hours" round-up."""
    text = sp._render_preview(
        {
            "direction": "long",
            "horizon_seconds": 10_800,  # 3 hours
            "conviction": "medium",
            "thesis_text": "scalp the dip",
        },
        {"symbol": "BTC", "coingecko_id": "bitcoin"},
    )
    assert "Horizon: 3 hours" in text


def test_render_preview_uses_placeholders_when_blank() -> None:
    text = sp._render_preview({}, {})
    # Placeholders mirror the matching "📝 Set X" button label so the
    # user's eye can jump from blank line to matching button without
    # translation. An unset field is an explicit missing-field action,
    # not an optional touch-up — "Set" rather than "Edit".
    assert "Asset: (tap 📝 Set Asset)" in text
    assert "Direction: (tap 📝 Set Direction)" in text
    assert "Horizon: (tap 📝 Set Horizon)" in text
    assert "Conviction: (tap 📝 Set Conviction)" in text
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
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "submitted_at": "2026-05-29T16:32:00Z",
            "resolves_at": "2026-06-12T16:32:00Z",
            "price_usd_at_submit": 2438.12,
        },
        draft={
            "direction": "long",
            "horizon_seconds": 1_209_600,
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
    # Entry-price line — the user must see the price they were
    # betting against; thousands separator + 2dp for >= $1 tickers.
    assert "Entry price: $2,438.12 per ETH" in text
    # Full UTC date+time, human form (not "May 29", not "in 2 weeks").
    assert "May 29 @ 16:32 UTC" in text
    assert "June 12 @ 16:32 UTC" in text
    # Pointer to where the user can see open calls.
    assert "/me" in text


def test_render_receipt_omits_entry_price_when_not_captured() -> None:
    """If CoinGecko was down at submit, ``price_usd_at_submit`` is
    ``None`` and the receipt must omit the line entirely. The bot
    never invents a placeholder, never shows "Entry price: $0.00"
    (which would lie about the market state).
    """
    text = sp._render_receipt(
        result={
            "asset_symbol": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "submitted_at": "2026-05-29T16:32:00Z",
            "resolves_at": "2026-06-12T16:32:00Z",
            "price_usd_at_submit": None,
        },
        draft={"direction": "long"},
        asset={"symbol": "ETH"},
    )
    assert "Entry price" not in text
    assert "$0" not in text  # no placeholder leaks


def test_render_receipt_entry_price_adaptive_decimals_subdollar() -> None:
    """Memecoins / micro-caps need more decimals to be meaningful —
    a $0.000123 entry must NOT round to $0.00. The threshold matches
    the /me handler so a user sees the same number in both places."""
    text = sp._render_receipt(
        result={
            "asset_symbol": "DOGE",
            "direction": "long",
            "horizon_seconds": 86_400,
            "conviction": "weak",
            "thesis_text": "memecoin season",
            "submitted_at": "2026-05-29T16:32:00Z",
            "resolves_at": "2026-05-30T16:32:00Z",
            "price_usd_at_submit": 0.000123,
        },
        draft={},
        asset={"symbol": "DOGE"},
    )
    assert "Entry price: $0.00012300 per DOGE" in text


def test_render_receipt_entry_price_adaptive_decimals_midrange() -> None:
    """Mid-range tickers (sub-dollar but not micro-cap) render at
    4dp so the user gets a meaningful number without trailing zeros
    crowding the line."""
    text = sp._render_receipt(
        result={
            "asset_symbol": "RNDR",
            "direction": "long",
            "horizon_seconds": 604_800,
            "conviction": "medium",
            "thesis_text": "AI tokens running",
            "submitted_at": "2026-05-29T16:32:00Z",
            "resolves_at": "2026-06-05T16:32:00Z",
            "price_usd_at_submit": 0.4231,
        },
        draft={},
        asset={"symbol": "RNDR"},
    )
    assert "Entry price: $0.4231 per RNDR" in text


def test_utc_datetime_normalises_offset_timestamps() -> None:
    """ISO strings with an explicit offset (``+02:00``, etc.) must be
    converted to UTC, not displayed verbatim — otherwise different
    servers in different TZs would print different times."""
    text = sp._utc_datetime("2026-06-12T18:32:00+02:00")
    assert text == "June 12 @ 16:32 UTC"


def test_utc_datetime_assumes_utc_on_naive_input() -> None:
    text = sp._utc_datetime("2026-06-12T16:32:00")
    assert text == "June 12 @ 16:32 UTC"


def test_utc_datetime_handles_z_suffix() -> None:
    text = sp._utc_datetime("2026-06-12T16:32:00Z")
    assert text == "June 12 @ 16:32 UTC"


def test_utc_datetime_renders_human_month_name_with_single_digit_day() -> None:
    """``June 5`` not ``June 05`` — pinned explicitly so a regression
    to ``%Y-%m-%d`` is loud."""
    text = sp._utc_datetime("2026-06-05T09:07:00Z")
    assert text == "June 5 @ 09:07 UTC"


def test_utc_datetime_returns_soon_for_garbage_input() -> None:
    assert sp._utc_datetime("") == "soon"
    assert sp._utc_datetime("not-a-date") == "soon"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (3_600, "1 hour"),
        (10_800, "3 hours"),
        (86_400, "24 hours"),
        (604_800, "1 week"),
        (1_209_600, "2 weeks"),
        # 30 days renders as "30 days" (ambiguous "this month" vs
        # "exactly 30 days"); humanise_horizon keeps the explicit
        # form so the preview matches what the user can text back.
        (2_592_000, "30 days"),
        (7_776_000, "3 months"),
    ],
)
def test_horizon_human_maps_seconds(seconds: int, expected: str) -> None:
    """``_horizon_human`` must accept integer seconds and render the
    user-facing label via the domain humaniser. Replaces the old
    Literal-code mapping after the continuous-horizon migration."""
    assert sp._horizon_human(seconds) == expected


def test_horizon_human_unknown_returns_placeholder() -> None:
    """Defensive: missing / non-numeric / zero / negative values
    must NOT crash the preview — they render the same Set Horizon
    placeholder the empty draft does. Pins the contract for stale
    drafts that survive a schema-shape change."""
    assert sp._horizon_human(None) == "(tap 📝 Set Horizon)"
    assert sp._horizon_human("") == "(tap 📝 Set Horizon)"
    assert sp._horizon_human(0) == "(tap 📝 Set Horizon)"
    assert sp._horizon_human("garbage") == "(tap 📝 Set Horizon)"


@pytest.mark.parametrize("code,expected", [("long", "Long"), ("short", "Short")])
def test_direction_human(code: str, expected: str) -> None:
    assert sp._direction_human(code) == expected


def test_direction_human_unknown_returns_placeholder() -> None:
    assert sp._direction_human(None) == "(tap 📝 Set Direction)"


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
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "asset_query": "ETH",
            "extracted_fields": {"schema_version": 1, "rumour_referenced": True},
            "parse_metadata": {"parsed_via": "freeform_llm"},
        },
        {"coingecko_id": "ethereum", "symbol": "ETH"},
        tg_user_id=42,
    )
    assert payload is not None
    assert payload["coingecko_id"] == "ethereum"
    assert payload["direction"] == "long"
    assert payload["horizon_seconds"] == 1_209_600
    assert payload["tg_user_id"] == 42
    # ``asset_query`` rides through so the audit row can reconstruct
    # the LLM-extracted symbol → coingecko_id mapping.
    assert payload["asset_query"] == "ETH"
    assert payload["extracted_fields"]["rumour_referenced"] is True


def test_build_submit_payload_missing_required_field_returns_none() -> None:
    payload = sp._build_submit_payload(
        {"direction": "long", "horizon_seconds": 1_209_600},  # no conviction or thesis
        {"coingecko_id": "ethereum"},
        tg_user_id=42,
    )
    assert payload is None


def test_build_submit_payload_no_asset_returns_none() -> None:
    payload = sp._build_submit_payload(
        {
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "ok",
        },
        {},
        tg_user_id=42,
    )
    assert payload is None


def test_build_submit_payload_missing_tg_user_id_returns_none() -> None:
    """The API needs the caller's TG id to resolve a source_id. An
    Update with no ``effective_user`` (e.g. a channel post replay)
    means the bot can't make this call — better to drop the payload
    than send a half-populated body."""
    payload = sp._build_submit_payload(
        {
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "ok",
        },
        {"coingecko_id": "ethereum"},
        tg_user_id=None,
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
    "horizon_seconds": 1_209_600,
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
    ["direction", "horizon_seconds", "conviction", "thesis_text"],
)
def test_keyboard_hides_confirm_when_draft_field_missing(missing_field: str) -> None:
    """User MUST NOT be able to confirm an incomplete bet — the
    Confirm button does not render until every required field is set."""
    draft: dict[str, object] = dict(_FULL_DRAFT)
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


def test_keyboard_uses_set_labels_for_empty_draft() -> None:
    """An empty draft is "missing-field" territory — every action
    button reads "📝 Set X", not "✏️ Edit X". The pencil emoji
    suggests an optional touch-up; users reported they didn't
    realise the fields were *required* until Confirm refused to
    render. The clipboard + "Set" verb signals "you have to put
    something here first".

    Cancel must remain reachable even when the draft is incomplete
    — otherwise the user has no way to bail.
    """
    kb = sp._build_preview_keyboard({}, {})
    labels = _button_labels(kb)
    for expected in [
        "📝 Set Asset",
        "📝 Set Direction",
        "📝 Set Horizon",
        "📝 Set Conviction",
        "📝 Set Thesis",
        "❌ Cancel",
    ]:
        assert expected in labels, f"missing button: {expected!r} in {labels!r}"
    # And none of the "Edit X" flavours leak in — the discriminator
    # is set-vs-edit, not both.
    for forbidden in [
        "✏️ Edit Asset",
        "✏️ Edit Direction",
        "✏️ Edit Horizon",
        "✏️ Edit Conviction",
        "✏️ Edit Thesis",
    ]:
        assert forbidden not in labels, (
            f"unset field rendered the 'Edit' label {forbidden!r}; "
            "empty fields must read '📝 Set X' so the user knows it's missing"
        )


def test_keyboard_uses_edit_labels_when_fields_are_set() -> None:
    """Mirror of the empty case: a fully-populated draft renders
    "✏️ Edit X" for every field. The pencil reads as "you can
    change what's here"; we never downgrade a populated field to
    "Set" (that would be misleading)."""
    kb = sp._build_preview_keyboard(_FULL_DRAFT, _FULL_ASSET)
    labels = _button_labels(kb)
    for expected in [
        "✏️ Edit Asset",
        "✏️ Edit Direction",
        "✏️ Edit Horizon",
        "✏️ Edit Conviction",
        "✏️ Edit Thesis",
    ]:
        assert expected in labels, f"missing button: {expected!r} in {labels!r}"
    # And the "Set X" variants are absent — a populated field is
    # never an explicit "missing" action.
    for forbidden in [
        "📝 Set Asset",
        "📝 Set Direction",
        "📝 Set Horizon",
        "📝 Set Conviction",
        "📝 Set Thesis",
    ]:
        assert forbidden not in labels


def test_keyboard_mixes_set_and_edit_per_field() -> None:
    """Per-field discriminator: a draft with some fields set and
    some empty must render "Edit" on the set ones and "Set" on the
    empty ones — the button labels reflect the *per-field* state,
    not a global toggle."""
    partial_draft = {
        "direction": "long",
        # No horizon_seconds → "Set Horizon"
        "conviction": "strong",
        "thesis_text": "",  # blank → "Set Thesis"
    }
    kb = sp._build_preview_keyboard(partial_draft, _FULL_ASSET)
    labels = _button_labels(kb)
    assert "✏️ Edit Asset" in labels  # asset is set
    assert "✏️ Edit Direction" in labels  # direction is set
    assert "📝 Set Horizon" in labels  # horizon empty
    assert "✏️ Edit Conviction" in labels  # conviction set
    assert "📝 Set Thesis" in labels  # thesis empty


def test_is_draft_complete_mirrors_submit_payload_preconditions() -> None:
    """``_is_draft_complete`` is the keyboard's gate; ``_build_submit_payload``
    is the on-confirm validator. They MUST agree — if the keyboard
    shows Confirm, the submit MUST succeed; if hidden, MUST fail.

    ``_build_submit_payload`` additionally requires ``tg_user_id`` (the
    API needs it to resolve the source); ``_is_draft_complete``
    doesn't gate on it because the user-facing card has no field that
    surfaces "TG id missing". They agree on the draft+asset
    preconditions, which is what the keyboard renders against.
    """
    tg = 42
    # Complete → both pass.
    assert sp._is_draft_complete(_FULL_DRAFT, _FULL_ASSET) is True
    assert sp._build_submit_payload(_FULL_DRAFT, _FULL_ASSET, tg_user_id=tg) is not None
    # Missing asset → both refuse.
    assert sp._is_draft_complete(_FULL_DRAFT, {}) is False
    assert sp._build_submit_payload(_FULL_DRAFT, {}, tg_user_id=tg) is None
    # Missing horizon → both refuse.
    bad = dict(_FULL_DRAFT, horizon_seconds=None)
    assert sp._is_draft_complete(bad, _FULL_ASSET) is False
    assert sp._build_submit_payload(bad, _FULL_ASSET, tg_user_id=tg) is None
