"""``send_first_call_nudges()`` — activation nudge cron (Q-031).

DMs onboarded testers who have a name but zero calls, prompting their
first bet. The per-user limits (at most once a week, at most twice
ever) live in Postgres (``engagement_nudges``), never in evictable
Redis — the durability lesson from Q-030.

Eligibility + both limits are enforced in one SQL query
(``EngagementRepository.list_first_call_nudge_targets``). For each
eligible source we RECORD the nudge (insert + commit) BEFORE sending:
record-then-send means a crash between the two costs at most one
*missed* nudge, never a duplicate / spam — the strictly safer failure
for an unsolicited DM. Per-user errors are isolated so one
unreachable / blocked user doesn't stop the rest.

Driven by the RQ ``CronScheduler`` (see ``merlin_worker.schedules.cron``);
runs on boot for an immediate pass. Imports happen lazily inside the
function so RQ's pickle dispatch doesn't choke on module-level state —
same discipline as the other jobs.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("merlin.worker.jobs.first_call_nudge")

# Upper bound on nudges enqueued per tick. Internal safety rail (not an
# operator knob) — at v0 volume the eligible set is a handful; this just
# stops a pathological backlog from sending thousands of DMs in one tick.
_NUDGE_BATCH_LIMIT = 500


def send_first_call_nudges() -> int:
    """Sync entry point called by RQ. Returns the number of nudges sent."""
    return asyncio.run(run_first_call_nudges())


async def run_first_call_nudges() -> int:
    """Send the first-call nudge to every currently-eligible source."""
    from merlin_infrastructure.db.repositories import (
        FIRST_CALL_NUDGE_KIND,
        EngagementRepository,
        SourceIdentityRepository,
    )
    from merlin_infrastructure.db.session import get_session_factory
    from merlin_infrastructure.notify.telegram.notifier import TelegramNotifier
    from merlin_infrastructure.notify.telegram.templates import first_call_nudge

    from merlin_worker.config import Settings

    settings = Settings()
    session_factory = get_session_factory(settings.database_url)

    # 1. Eligibility query (own read-only session). Honors rules 1-3 +
    #    the settle-in guard entirely in SQL.
    async with session_factory() as session:
        targets = await EngagementRepository(session).list_first_call_nudge_targets(
            cooldown_seconds=settings.first_call_nudge_cooldown_s,
            max_sends=settings.first_call_nudge_max_sends,
            min_account_age_seconds=settings.first_call_nudge_min_account_age_s,
            limit=_NUDGE_BATCH_LIMIT,
        )

    if not targets:
        _log.info("first-call nudge: no eligible users")
        return 0

    # 2. Resolve opaque source_ids -> raw Telegram ids through the PII
    #    repo (MVP §15 invariant 9 — the only place that read lives).
    async with session_factory() as session:
        tg_by_source = await SourceIdentityRepository(session).get_tg_user_ids_by_source_ids(
            targets,
        )

    notifier = TelegramNotifier(bot_token=settings.telegram_bot_token or "noop")
    text = first_call_nudge()
    sent = 0

    for source_id in targets:
        tg_user_id = tg_by_source.get(source_id)
        if tg_user_id is None:
            # Source with no identity row (shouldn't happen on the
            # onboarding path) — skip rather than guess a chat id.
            _log.warning(
                "first-call nudge: no identity row for source %s — skipping",
                source_id,
            )
            continue

        # Record BEFORE sending, each in its own committed TX. If the
        # process dies between the commit and the send, the user misses
        # one nudge (acceptable) instead of getting re-nudged on the
        # next tick (spam). A record failure skips the send entirely so
        # we never DM without accounting for the cap/cooldown.
        try:
            async with session_factory() as session:
                await EngagementRepository(session).record_nudge(
                    source_id=source_id,
                    nudge_kind=FIRST_CALL_NUDGE_KIND,
                )
                await session.commit()
        except Exception:
            _log.exception(
                "first-call nudge: failed to record nudge for source %s — skipping send",
                source_id,
            )
            continue

        # notify_user swallows + logs all transport errors, so a single
        # blocked user can't raise into this loop.
        await notifier.notify_user(tg_user_id, text)
        sent += 1

    _log.info("first-call nudge: sent %d", sent)
    return sent
