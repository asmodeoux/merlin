r"""Operator recovery CLI — re-enqueue lost resolution jobs (Q-030).

Two subcommands, both safe to re-run: ``resolve_outcome`` short-circuits
when an outcome row already exists, and resolution reads *historical*
CoinGecko prices at the claim's original ``t0`` / ``t_resolve`` — so a
late re-fire is still correct, just delayed.

* ``rerun <claim_id>`` — re-enqueue resolution for one specific claim.
  This is the single-bet path the docs have long promised as
  ``make admin-rerun CLAIM_ID=...`` (it never actually existed).
* ``backfill [--grace-seconds N]`` — run the reconciliation sweep once,
  re-enqueueing *every* overdue, unresolved claim. ``make admin-backfill``.

Run locally against the dev stack, or in prod via::

    fly ssh console -a merlin-worker \
      -C "uv run --package merlin-worker python -m merlin_worker.admin backfill"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from uuid import UUID

_log = logging.getLogger("merlin.worker.admin")


def _rerun(claim_id: str) -> str:
    """Re-enqueue ``resolve_outcome`` for one claim. Returns the job id."""
    from merlin_infrastructure.queue.names import RESOLUTION_QUEUE_NAME
    from merlin_infrastructure.queue.rq import RQQueueAdapter
    from redis import Redis

    from merlin_worker.config import Settings

    try:
        UUID(claim_id)
    except ValueError as e:
        raise SystemExit(f"admin rerun: {claim_id!r} is not a valid claim UUID") from e

    settings = Settings()
    redis_url = os.environ.get("REDIS_URL", settings.redis_url)
    queue = RQQueueAdapter(
        redis=Redis.from_url(redis_url),
        queue_name=RESOLUTION_QUEUE_NAME,
    )
    return queue.enqueue(
        "merlin_worker.jobs.resolve_outcome",
        claim_id,
        job_id=f"resolve-{claim_id}",
    )


def _backfill(grace_seconds: int | None) -> int:
    """Run the reconciliation sweep once. Returns the re-enqueued count."""
    from merlin_worker.jobs.sweep_unresolved import run_sweep

    return asyncio.run(run_sweep(grace_seconds=grace_seconds))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    logging.basicConfig(level="INFO")
    parser = argparse.ArgumentParser(
        prog="merlin_worker.admin",
        description="Resolution recovery tools (Q-030).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_rerun = sub.add_parser("rerun", help="Re-enqueue resolution for one claim id.")
    p_rerun.add_argument("claim_id", help="The claim UUID to re-resolve.")

    p_backfill = sub.add_parser(
        "backfill",
        help="Re-enqueue every overdue, unresolved claim (the Q-030 sweep).",
    )
    p_backfill.add_argument(
        "--grace-seconds",
        type=int,
        default=None,
        help="Override the overdue grace window (default: worker Settings, 6h).",
    )

    args = parser.parse_args(argv)

    if args.command == "rerun":
        job_id = _rerun(args.claim_id)
        print(f"re-enqueued resolve_outcome for claim {args.claim_id} (job {job_id})")
        return 0

    # The only other registered subcommand (subparsers are required).
    count = _backfill(args.grace_seconds)
    print(f"resolution backfill: re-enqueued {count} overdue unresolved claim(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
