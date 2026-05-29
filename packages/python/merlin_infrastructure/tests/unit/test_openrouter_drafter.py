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
            "horizon": "14d",
            "conviction": "strong",
            "thesis_text": "Vitalik rumour",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon": 0.9,
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
    assert draft.horizon == "14d"
    assert draft.conviction == "strong"
    assert draft.thesis_text == "Vitalik rumour"
    assert draft.extracted_fields.rumour_referenced is True
    assert draft.extracted_fields.named_entities == ("Vitalik",)
    assert draft.extracted_fields.target_price_usd == 4000.0
    assert draft.parse_metadata is not None
    assert draft.parse_metadata.parsed_via == "freeform_llm"
    assert draft.parse_metadata.raw_user_turns == ("ETH long 14d strong, Vitalik rumour",)
    assert draft.missing_fields == ()


async def test_low_confidence_field_is_marked_missing(
    http_client: httpx.AsyncClient,
) -> None:
    drafter = OpenRouterClaimDrafter(api_key="sk-test", http_client=http_client)
    body = _completion_payload(
        {
            "asset_query": "ETH",
            "direction": "long",
            "horizon": "14d",
            "conviction": "strong",
            "confidences": {
                "asset_query": 0.95,
                "direction": 0.95,
                "horizon": 0.3,  # below floor
                "conviction": 0.9,
            },
        }
    )
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        draft = await drafter.draft("ETH long sometime soon strong")

    assert draft.horizon is None
    assert "horizon" in draft.missing_fields


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
                "horizon": "14d",
                "conviction": "strong",
                "confidences": {
                    "asset_query": 0.95,
                    "direction": 0.99,
                    "horizon": 0.95,
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
    assert draft.horizon == "14d"
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
