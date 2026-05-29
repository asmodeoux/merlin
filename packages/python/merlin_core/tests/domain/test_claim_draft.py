"""Unit tests for the ClaimDraft domain dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from merlin_core.domain.claim_draft import (
    AssetCandidate,
    ClaimDraft,
    ExtractedFields,
    ParseMetadata,
)


def test_asset_candidate_is_frozen() -> None:
    cand = AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum")
    with pytest.raises(AttributeError):
        cand.symbol = "BTC"  # type: ignore[misc]


def test_extracted_fields_defaults_match_schema() -> None:
    f = ExtractedFields()
    assert f.schema_version == 1
    assert f.target_price_usd is None
    assert f.rumour_referenced is False
    assert f.named_entities == ()
    assert f.raw_extras == {}


def test_parse_metadata_preserves_raw_user_turn() -> None:
    pm = ParseMetadata(
        parsed_via="freeform_llm",
        model="openai/gpt-5-mini",
        raw_user_turns=("ETH long 14d strong",),
        extracted_at=datetime(2026, 5, 29, tzinfo=UTC),
    )
    assert pm.raw_user_turns[0] == "ETH long 14d strong"
    assert pm.parsed_via == "freeform_llm"


def test_claim_draft_empty_defaults() -> None:
    d = ClaimDraft()
    assert d.asset_candidates == ()
    assert d.direction is None
    assert d.horizon is None
    assert d.conviction is None
    assert d.thesis_text is None
    assert d.missing_fields == ()
    assert d.extracted_fields.schema_version == 1


def test_claim_draft_full_round_trip() -> None:
    draft = ClaimDraft(
        asset_candidates=(AssetCandidate(coingecko_id="ethereum", symbol="ETH", name="Ethereum"),),
        direction="long",
        horizon="14d",
        conviction="strong",
        thesis_text="Vitalik rumour",
        extracted_fields=ExtractedFields(rumour_referenced=True),
        parse_metadata=ParseMetadata(
            parsed_via="freeform_llm",
            raw_user_turns=("ETH long 14d strong, Vitalik rumour",),
        ),
    )
    assert draft.direction == "long"
    assert draft.extracted_fields.rumour_referenced is True
    assert draft.parse_metadata is not None
    assert draft.parse_metadata.raw_user_turns[0].startswith("ETH long")
