"""Unit tests for ``OpenRouterClaimDrafter`` via ``respx``."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from merlin_core.application.ports import ClaimDrafterError
from merlin_infrastructure.llm.openrouter import OpenRouterClaimDrafter


def _completion_payload(content: dict, *, model: str = "openai/gpt-5-mini") -> dict:
    return {
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(content)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.fixture
async def http_client() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client


async def test_successful_extraction_high_confidence(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.9,
                "conviction": 0.85,
            },
            "extracted_fields": {
                "schema_version": 1,
                "target_price_usd": 4000,
                "rumour_referenced": True,
                "named_entities": ["Vitalik"],
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 14d strong, Vitalik rumour")

    assert draft.direction == "long"
    assert draft.horizon == 1_209_600
    assert draft.conviction == "strong"
    assert draft.thesis_text == "Vitalik rumour"
    assert draft.extracted_fields.rumour_referenced is True
    assert draft.extracted_fields.named_entities == ("Vitalik",)
    assert draft.extracted_fields.target_price_usd == 4000.0
    assert draft.parse_metadata is not None
    assert draft.parse_metadata.parsed_via == "freeform_llm"
    assert draft.parse_metadata.raw_user_turns == ("ETH long 14d strong, Vitalik rumour",)
    assert draft.missing_fields == ()
    # REGRESSION (Sprint 1 live bug): the LLM-extracted asset_query
    # MUST survive the round-trip onto the returned ClaimDraft, so the
    # API can pass it to CoinGecko /search instead of guessing with
    # thesis_text. Dropping it on the floor here caused obvious calls
    # like "ETH long 2 weeks, Vitalik staking news" to land with zero
    # asset candidates.
    assert draft.asset_query == "ETH"


async def test_asset_query_below_confidence_floor_is_none(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM is unsure of the asset, ``asset_query`` is None
    AND ``asset`` appears in ``missing_fields``. The API then opens
    the asset picker instead of running a CoinGecko search against a
    low-confidence guess."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "maybe ETH idk",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.3,  # below 0.85 floor
                "direction": 0.95,
                "horizon_seconds": 0.9,
                "conviction": 0.85,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("something market-y, maybe ETH idk")

    assert draft.asset_query is None
    assert "asset" in draft.missing_fields


async def test_low_confidence_field_is_marked_missing(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM's horizon confidence is below the floor AND the
    user text has no explicit duration to backstop from, ``horizon``
    lands in ``missing_fields`` so the bot opens the picker.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.3,  # below floor
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        # Deliberately no "for / in / within / over <N> <unit>" phrase
        # in the user text so the backstop has nothing to recover.
        draft = await drafter.draft("ETH long sometime soon strong")

    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


async def test_low_floor_lets_well_calibrated_horizon_through(
    http_client: httpx.AsyncClient,
) -> None:
    """A 0.75 horizon confidence — the band where gpt-5-mini sits on
    a hedged-but-explicit duration like "for 2 hours, must recover" —
    must pass the floor. Pins the regression that prompted lowering
    the floor from 0.85 to 0.70.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "SOL",
            "direction": "long",
            "horizon_seconds": 7_200,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.75,
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("SOL long for 2 hours, it must recover from a huge drop")
    assert draft.horizon == 7_200
    assert "horizon" not in draft.missing_fields


async def test_backstop_recovers_horizon_from_user_text_on_null_emission(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM returns ``horizon_seconds: null`` despite an
    unambiguous "for N <unit>" phrase in the user text, the
    deterministic regex backstop recovers the value via the canonical
    domain parser. Without the backstop, the user gets a "Set Horizon"
    placeholder on an input that's as explicit as language gets.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "SOL",
            "direction": "long",
            "horizon_seconds": None,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.0,  # model gave up on horizon
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("SOL long for 2 hours, it must recover from a huge drop")
    assert draft.horizon == 7_200
    assert "horizon" not in draft.missing_fields


async def test_backstop_recovers_horizon_when_confidence_culled(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM emits a horizon value but with confidence below
    the floor, the gate culls it AND the backstop recovers a clean
    duration from the user text. End-to-end, the user sees the right
    horizon despite the model's hesitancy.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 7_200,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.4,  # below 0.70 floor
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long within 2 hours, alt season starts")
    assert draft.horizon == 7_200
    assert "horizon" not in draft.missing_fields


async def test_backstop_does_not_override_high_confidence_llm_value(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM's horizon passes the floor, the backstop does NOT
    run — the model's reading trumps a regex (it could disambiguate
    things like "I want resolution within 24 hours after the 2-hour
    recovery window" → 86400, not 7200).
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 86_400,  # 24h — model's reading
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.95,
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        # Text contains "for 2 hours" — if the backstop ran, it would
        # mis-overrule the model's 86400 with 7200.
        draft = await drafter.draft(
            "ETH long, I want resolution within 24 hours after the 2 hours recovery"
        )
    assert draft.horizon == 86_400


async def test_backstop_skips_out_of_range_match_and_falls_through(
    http_client: httpx.AsyncClient,
) -> None:
    """``"for 6 months"`` is 180 days — outside the 90-day ceiling.
    The backstop skips that match (rather than clamping) and falls
    through to the next match. If there's no in-range fallback,
    horizon stays missing and the bot opens the picker.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": None,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.0,
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long for 6 months, fundamentals story")
    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


async def test_backstop_ignores_referential_durations(
    http_client: httpx.AsyncClient,
) -> None:
    """``"after"`` / ``"next"`` / ``"by"`` are excluded from the
    backstop preposition set because they often introduce time
    references that are NOT the user's horizon ("after 2 hours of
    analysis"). The backstop must NOT mistake a referential phrase
    for a horizon when the user didn't give an explicit "for / in /
    within / over <N> <unit>".
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": None,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.99,
                "direction": 0.98,
                "horizon_seconds": 0.0,
                "conviction": 0.8,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long, after 2 hours of analysis I think it pumps")
    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


async def test_low_confidence_conviction_defaults_to_medium(
    http_client: httpx.AsyncClient,
) -> None:
    """Conviction reads tone, not keywords — the model gets it right less
    reliably than direction/horizon. When confidence is below the floor we
    default to ``"medium"`` (the centre of the four-value scale) instead of
    surfacing it as a missing field; the user can still retune it via the
    preview's Edit button."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.9,
                "conviction": 0.3,  # below 0.7 floor
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 2 weeks, might pop")

    assert draft.conviction == "medium"
    assert "conviction" not in draft.missing_fields


async def test_null_conviction_defaults_to_medium(
    http_client: httpx.AsyncClient,
) -> None:
    """When the LLM emits ``conviction: null`` (no signal in the message),
    we still hand the preview a usable default rather than asking the user
    to pick one. See ``_CONVICTION_DEFAULT``."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": None,
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.9,
                "conviction": 0.0,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 2 weeks")

    assert draft.conviction == "medium"
    assert "conviction" not in draft.missing_fields


async def test_out_of_domain_conviction_defaults_to_medium(
    http_client: httpx.AsyncClient,
) -> None:
    """If the LLM ignores the enum and returns something like
    ``"moderate"``, ``_typed`` rejects it; we then default to ``"medium"``
    rather than dropping the field on the floor with a misleading
    high-confidence audit trail."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 1_209_600,
            "conviction": "moderate",  # not in {weak, medium, strong, very_strong}
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.9,
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 2 weeks moderate")

    assert draft.conviction == "medium"
    assert "conviction" not in draft.missing_fields


async def test_5xx_raises_claim_drafter_error(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(503))
        with pytest.raises(ClaimDrafterError):
            await drafter.draft("ETH long")


async def test_429_raises_rate_limit_error(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(429))
        with pytest.raises(ClaimDrafterError, match="rate-limited"):
            await drafter.draft("ETH long")


async def test_non_json_content_raises(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = {
        "model": "google/gemini-3.5-flash",
        "choices": [{"message": {"content": "not json at all"}}],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClaimDrafterError, match="non-JSON"):
            await drafter.draft("ETH long")


async def test_empty_text_short_circuits_without_http_call(
    http_client: httpx.AsyncClient,
) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    with respx.mock(
        base_url="https://openrouter.ai/api/v1",
        assert_all_called=False,
    ) as router:
        route = router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=_completion_payload({}))
        )
        draft = await drafter.draft("   ")
    assert route.call_count == 0
    assert draft.direction is None


async def test_prompt_hash_is_deterministic(http_client: httpx.AsyncClient) -> None:
    a = OpenRouterClaimDrafter(api_key="sk-1", http_client=http_client)
    b = OpenRouterClaimDrafter(api_key="sk-2", http_client=http_client)
    # prompt_hash is private but exposed via the recorded ParseMetadata.
    assert a._prompt_hash == b._prompt_hash


async def test_null_content_raises_with_finish_reason(
    http_client: httpx.AsyncClient,
) -> None:
    """gpt-5 / o-series sometimes return content: null when the
    reasoning budget consumed max_tokens. Surface finish_reason so the
    cause is obvious from the log."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = {
        "model": "openai/gpt-5-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": None},
                "finish_reason": "length",
            }
        ],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClaimDrafterError, match="finish_reason='length'"):
            await drafter.draft("ETH long")


async def test_empty_string_content_raises(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = {
        "model": "google/gemini-3.5-flash",
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClaimDrafterError, match="empty content"):
            await drafter.draft("ETH long")


async def test_refusal_field_appears_in_error(http_client: httpx.AsyncClient) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = {
        "model": "openai/gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "refusal": "I cannot help with that request.",
                },
                "finish_reason": "stop",
            }
        ],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ClaimDrafterError, match="I cannot help"):
            await drafter.draft("ETH long")


async def test_markdown_fenced_json_is_stripped(http_client: httpx.AsyncClient) -> None:
    """Some models wrap JSON in ```json ... ``` despite response_format hints."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "asset_query": "ETH",
                "direction": "long",
                "horizon_seconds": 1_209_600,
                "conviction": "strong",
                "confidences": {
                    "asset_query": 0.95,
                    "direction": 0.99,
                    "horizon_seconds": 0.95,
                    "conviction": 0.9,
                },
            }
        )
        + "\n```"
    )
    body = {
        "model": "google/gemini-3.5-flash",
        "choices": [{"message": {"content": fenced}, "finish_reason": "stop"}],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 14d strong")
    assert draft.direction == "long"
    assert draft.horizon == 1_209_600
    assert draft.conviction == "strong"


async def test_plain_code_fence_without_language_tag_is_stripped(
    http_client: httpx.AsyncClient,
) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    fenced = "```\n" + json.dumps({"direction": None, "confidences": {}}) + "\n```"
    body = {
        "model": "google/gemini-3.5-flash",
        "choices": [{"message": {"content": fenced}, "finish_reason": "stop"}],
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ambiguous text")
    assert draft.direction is None  # confidences empty → gated out


async def test_legacy_bucket_string_horizon_is_coerced(http_client: httpx.AsyncClient) -> None:
    """Back-compat: if the model ignores the v2 prompt and emits the
    old bucket string (``"14d"``), we still extract the value via the
    domain parser instead of dropping it on the floor.

    The new key ``horizon_seconds`` is preferred, but the adapter also
    accepts the legacy ``horizon`` key during prompt transitions so a
    stale model snapshot doesn't break extraction.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon": "14d",  # legacy key + legacy string value
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon": 0.95,
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 2 weeks strong")
    assert draft.horizon == 1_209_600


async def test_below_range_horizon_is_gated_to_none(http_client: httpx.AsyncClient) -> None:
    """Model emitting a value below the 1-hour floor must NOT slip
    through — surface as a missing field so the bot opens the picker.

    Regression-pin for the boundary: we'd rather force the user to
    pick a longer horizon than commit a 60-second bet that CoinGecko's
    5-minute bucket can't resolve cleanly.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 60,  # 1 minute — below 1h floor
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.95,
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 1 minute strong")
    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


async def test_above_range_horizon_is_gated_to_none(http_client: httpx.AsyncClient) -> None:
    """Same as above, but for the 90-day ceiling. The eventual
    leaderboard window assumes resolutions land within a quarter; a
    one-year bet would sit forever in the open list and never get
    rendered into a useful outcome.
    """
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon_seconds": 31_536_000,  # 1 year — above 90d ceiling
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon_seconds": 0.95,
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long 1 year strong")
    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


async def test_request_disables_reasoning(http_client: httpx.AsyncClient) -> None:
    """Extraction is one-shot pattern-matching — reasoning tokens are
    pure cost waste. The request body must opt out."""
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        route = router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=_completion_payload({}))
        )
        await drafter.draft("ETH long")
    assert route.calls.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["reasoning"] == {"effort": "minimal", "exclude": True}
    # Generous max_tokens to keep reasoning tokens from starving the
    # visible content out of the response.
    assert sent["max_tokens"] >= 2048
