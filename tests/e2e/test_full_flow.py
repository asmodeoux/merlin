"""End-to-end stub: ``/start`` → invite → freeform claim → time-travel → resolved.

This file is the placeholder for the full-flow test described in the
bot-flow plan §"Sprint 1 acceptance criteria" #1. The concrete suite
lands once the API/bot/worker have real DB-backed handlers; until then,
this skeleton documents the intended shape so reviewers can verify the
test contract evolves with the implementation.

The real test will:

1. Spin up the dev compose stack (postgres + redis + api + bot + worker).
2. Mock OpenRouter, CoinGecko, and Telegram with respx cassettes.
3. Drive a synthetic Telegram Update through the bot:
   - ``/start`` → invite redeem → display name.
   - freeform message ("ETH long 14d strong, Vitalik rumour").
   - tap Confirm on the preview.
4. Assert the API persisted ``claims``/``events``/``hypotheses``/
   ``attribution_ledger`` rows and enqueued ``resolve-<claim_id>``.
5. Freeze the worker clock past ``t_resolve``.
6. Trigger the RQ worker once; assert ``outcomes`` row + bot-voice
   notification + downstream ``recompute_source_reputation`` job.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.slow
def test_full_flow_placeholder() -> None:
    """Skipped placeholder — see module docstring."""
    pytest.skip("Sprint 1 deliverable: implementation follows API/worker landing.")
