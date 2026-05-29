"""Manual smoke test for the OpenRouter LLM connection.

Reads ``apps/api/.env`` via the API's own ``Settings`` (the same path the
production code uses), constructs the real ``OpenRouterClaimDrafter``,
fires a single representative freeform message at the configured primary
model, then a second call against the fallback model. Reports status,
latency, and the extracted draft for each.

Run with::

    uv run --project apps/api python scripts/llm_smoke.py

Never wired into CI — this hits OpenRouter and costs real (tiny) money.
Stays out of pytest discovery for the same reason.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

# Point pydantic-settings at the API's .env regardless of where the
# script is run from.
_API_ENV = Path(__file__).resolve().parent.parent / "apps" / "api" / ".env"
os.environ.setdefault("MERLIN_API_ENV_FILE", str(_API_ENV))

from merlin_api.config import Settings  # noqa: E402
from merlin_core.application.ports import ClaimDrafterError  # noqa: E402
from merlin_infrastructure.llm.openrouter import OpenRouterClaimDrafter  # noqa: E402

FREEFORM_SAMPLE = "ETH long 2 weeks, strong conviction — Vitalik rumour incoming"


async def _try_model(model_slug: str, *, api_key: str, base_url: str) -> int:
    print(f"\n── {model_slug} ──")
    async with httpx.AsyncClient(timeout=30.0) as client:
        drafter = OpenRouterClaimDrafter(
            api_key=api_key,
            base_url=base_url,
            primary_model=model_slug,
            # Force the same model on both slots so we test ONE model
            # at a time — no auto-routing surprises.
            fallback_model=model_slug,
            http_client=client,
        )
        t0 = time.perf_counter()
        try:
            draft = await drafter.draft(FREEFORM_SAMPLE)
        except ClaimDrafterError as e:
            print(f"  FAILED: {e}")
            return 1
        elapsed_ms = (time.perf_counter() - t0) * 1000

    print(f"  latency:        {elapsed_ms:6.0f} ms")
    print(f"  direction:      {draft.direction}")
    print(f"  horizon:        {draft.horizon}")
    print(f"  conviction:     {draft.conviction}")
    print(f"  thesis_text:    {draft.thesis_text!r}")
    cands = [c.coingecko_id for c in draft.asset_candidates] or "—"
    print(f"  candidates:     {cands}")
    print(f"  missing_fields: {draft.missing_fields or '—'}")
    if draft.parse_metadata is not None:
        print(f"  model_reported: {draft.parse_metadata.model}")
        print(f"  confidences:    {draft.parse_metadata.confidences}")
    return 0


async def main() -> int:
    """Probe primary + fallback LLM models and print per-model results."""
    settings = Settings(_env_file=str(_API_ENV))

    api_key = (settings.openrouter_api_key or "").strip()
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY is not set in apps/api/.env", file=sys.stderr)
        return 2

    print(f"input text: {FREEFORM_SAMPLE!r}")
    print(f"base_url:   {settings.openrouter_base_url}")
    print(f"primary:    {settings.llm_primary_model}")
    print(f"fallback:   {settings.llm_fallback_model}")

    rc_p = await _try_model(
        settings.llm_primary_model,
        api_key=api_key,
        base_url=settings.openrouter_base_url,
    )
    rc_f = await _try_model(
        settings.llm_fallback_model,
        api_key=api_key,
        base_url=settings.openrouter_base_url,
    )

    print("\n── summary ──")
    print(f"  primary ({settings.llm_primary_model}):  {'OK' if rc_p == 0 else 'FAIL'}")
    print(f"  fallback ({settings.llm_fallback_model}): {'OK' if rc_f == 0 else 'FAIL'}")
    return rc_p | rc_f


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
