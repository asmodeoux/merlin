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
    assert "ETH" in text
    assert "long" in text
    assert "2 weeks" in text
    assert "strong" in text
    assert "Vitalik rumour" in text
    assert "Confirm" in text or "Edit" in text


def test_render_preview_uses_placeholders_when_blank() -> None:
    text = sp._render_preview({}, {})
    assert "(tap Edit asset)" in text
    assert "(tap Edit direction)" in text
    assert "(tap Edit horizon)" in text
    assert "(tap Edit conviction)" in text


def test_render_receipt_includes_date_hint() -> None:
    text = sp._render_receipt(asset_symbol="ETH", resolves_at="2026-06-12T09:34:00Z")
    assert "ETH call resolved" not in text  # that's the outcome template, not the receipt
    assert "Resolves" in text
    assert "/me" in text


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
    assert sp._horizon_human(None) == "(tap Edit horizon)"
    assert sp._horizon_human("99y") == "(tap Edit horizon)"


@pytest.mark.parametrize("code,expected", [("long", "long"), ("short", "short")])
def test_direction_human(code: str, expected: str) -> None:
    assert sp._direction_human(code) == expected


def test_direction_human_unknown_returns_placeholder() -> None:
    assert sp._direction_human(None) == "(tap Edit direction)"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("weak", "weak"),
        ("medium", "medium"),
        ("strong", "strong"),
        ("very_strong", "very strong"),
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
