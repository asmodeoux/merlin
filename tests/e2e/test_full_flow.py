"""End-to-end stub: ``/start`` → invite → freeform claim → time-travel → resolved.

This file is the placeholder for the full-flow test described in the
bot-flow plan §"Sprint 1 acceptance criteria" #1. The first half — the
``/start`` → invite-redeem → ``source`` rows landed in Postgres path —
is **already covered** at the integration tier in
``apps/api/tests/integration/test_invite_flow.py`` (live testcontainer
Postgres, full lifecycle including revoke + race + expiry).

What's still ahead of this stub:

1. Drive a synthetic Telegram ``Update`` through the *bot* (not just
   the API), so ``ConversationHandler`` state transitions are
   exercised end-to-end.
2. Mock OpenRouter, CoinGecko, and Telegram with respx cassettes for
   the freeform claim path ("ETH long 14d strong, Vitalik rumour").
3. Tap Confirm on the preview; assert the API persisted
   ``claims`` / ``events`` / ``hypotheses`` / ``attribution_ledger``
   rows and enqueued ``resolve-<claim_id>``.
4. Freeze the worker clock past ``t_resolve``, trigger the RQ worker
   once, assert ``outcomes`` row + bot-voice notification +
   downstream ``recompute_source_reputation`` job.

Steps 3-4 require the worker's outcomes path, which lands in a later
sprint. Until then, this file remains a marker so reviewers can see
the missing slice; the invite-flow integration test is the live-DB
coverage today.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.slow
def test_full_flow_placeholder() -> None:
    """Skipped placeholder — see module docstring.

    Invite + onboarding leg of the full flow is exercised against a
    live Postgres in ``apps/api/tests/integration/test_invite_flow.py``;
    this stub remains for the claim → outcome → reputation half that
    depends on the worker's outcomes path.
    """
    pytest.skip("Outcomes worker not yet wired — see module docstring.")
