"""Unit tests for ``resolve_outcome`` — the resolver's hot path.

These tests mock the external collaborators (DB session factory,
``OutcomeRepository``, ``CoinGeckoAdapter``, ``TelegramNotifier``,
the inline reputation recompute, and the async fallback enqueue)
so the test never opens a real connection. The integration test
in ``apps/worker/tests/integration`` exercises the full sequence
against a testcontainers Postgres + a sync-mode RQ.

What we pin here:

1. Happy path — INSERTs a resolved outcome, recomputes reputation
   INLINE, threads the new ``weighted_pnl_pct`` into the won/lost
   notification, and never reaches for the async-enqueue fallback.
2. Idempotency — a second invocation against the same ``claim_id``
   short-circuits when an outcome row already exists. No double
   notification.
3. Q-023 — ``AssetNotFoundError`` from the market data adapter
   INSERTs a rejection row and sends the matching template; the
   reputation recompute is NOT triggered (the source's hit rate is
   untouched, the rejection template owns its own wording).
4. The async fallback enqueue lands on ``REPUTATION_QUEUE_NAME``
   (Sprint-1 queue-mismatch regression guard).
5. Inline-recompute failure recovery: a recompute exception MUST
   NOT block the user notification; the resolver falls back to the
   async enqueue and the notification body drops the PnL chunk
   (still includes the /me · /board CTA).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# Lightweight collaborators
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutcomeRepo:
    """In-memory ``OutcomeRepositoryPort`` stand-in for the resolver tests."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    existing_for_claim: dict[UUID, Any] = field(default_factory=dict)

    async def latest_for_claim(self, claim_id: UUID) -> Any:
        return self.existing_for_claim.get(claim_id)

    async def insert_resolved(self, **kwargs: Any) -> Any:
        kwargs["_kind"] = "resolved"
        self.rows.append(kwargs)
        return type("FakeRecord", (), {"id": uuid4(), "status": "resolved"})()

    async def insert_rejected_asset_mutation(self, **kwargs: Any) -> Any:
        kwargs["_kind"] = "rejected_asset_mutation"
        self.rows.append(kwargs)
        return type(
            "FakeRecord",
            (),
            {"id": uuid4(), "status": "rejected_asset_mutation"},
        )()


@dataclass
class _FakeSession:
    """Tracks commit() calls so the test can assert TX boundaries."""

    commits: int = 0

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


@dataclass
class _FakeSessionFactory:
    """Callable returning fresh ``_FakeSession`` instances.

    The resolver calls ``session_factory()`` for each TX boundary, so
    the fake mirrors that — a new session per ``async with``.
    """

    sessions: list[_FakeSession] = field(default_factory=list)

    def __call__(self) -> _FakeSession:
        s = _FakeSession()
        self.sessions.append(s)
        return s


@dataclass
class _FakePrice:
    price_usd: float
    returned_at: _dt.datetime


@dataclass
class _FakeMarket:
    """``MarketDataPort`` stand-in.

    ``prices`` maps a ``coingecko_id`` to a list of ``_FakePrice``
    values returned in order — first call returns the first element,
    second call the second, and so on. Mirrors the (t0, t_resolve)
    sequence the resolver issues.

    ``raise_asset_not_found`` flips the first call into a
    ``AssetNotFoundError`` so the Q-023 path is exercised.
    """

    prices: dict[str, list[_FakePrice]] = field(default_factory=dict)
    raise_asset_not_found: bool = False
    call_count: int = 0

    # Every call's ``when`` is recorded so tests can assert the resolver
    # asked for the historical price at ``t_resolve`` — not at "now". A
    # worker that wakes up six hours late MUST still score the bet using
    # the price at the expiry instant, otherwise late processing changes
    # outcomes. Pin this in test_uses_historical_price_at_t_resolve_not_now.
    requested_at: list[_dt.datetime] = field(default_factory=list)

    async def get_price_at(self, coingecko_id: str, when: _dt.datetime) -> _FakePrice:
        self.requested_at.append(when)
        if self.raise_asset_not_found:
            from merlin_core.application.ports import AssetNotFoundError

            raise AssetNotFoundError(coingecko_id)
        idx = self.call_count
        self.call_count += 1
        return self.prices[coingecko_id][idx]


@dataclass
class _FakeNotifier:
    """``NotifierPort`` stand-in — records every outbound message."""

    sent: list[tuple[int, str]] = field(default_factory=list)

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        self.sent.append((tg_user_id, text))


@dataclass
class _ResolverFixture:
    """Bundle of fakes returned by the ``resolver`` fixture.

    ``inline_recompute_calls`` and ``enqueued`` are the two reputation
    "did the resolver reach for me?" tracking surfaces. Happy path
    fires inline only; failure path fires inline (raise) + async
    fallback. Tests assert on both lists to pin which path
    exercised the resolver.
    """

    outcome_repo: _FakeOutcomeRepo
    market: _FakeMarket
    notifier: _FakeNotifier
    session_factory: _FakeSessionFactory
    enqueued: list[tuple[str, dict[str, Any]]]
    inline_recompute_calls: list[str]
    # Fixed return value the default inline-recompute stub hands back.
    # Tests that need a different value (or to make the stub raise)
    # override via ``monkeypatch.setattr(resolve_mod, "_recompute_async", ...)``.
    DEFAULT_INLINE_PNL: float = 6.13


@pytest.fixture
def resolver_fixture(monkeypatch: pytest.MonkeyPatch) -> _ResolverFixture:
    """Wire fakes into the resolver module's import sites."""
    outcome_repo = _FakeOutcomeRepo()
    market = _FakeMarket()
    notifier = _FakeNotifier()
    session_factory = _FakeSessionFactory()
    enqueued: list[tuple[str, dict[str, Any]]] = []
    inline_recompute_calls: list[str] = []

    # The package ``__init__`` re-exports ``resolve_outcome`` as a
    # function name in ``merlin_worker.jobs``, which shadows the
    # submodule attribute under any ``getattr``-driven lookup
    # (including ``monkeypatch.setattr("dotted.path", ...)``).
    # ``importlib.import_module`` registers the real submodule under
    # ``sys.modules`` so monkeypatches against the module attribute
    # land on the module object instead of the re-exported function.
    import importlib
    import sys

    importlib.import_module("merlin_worker.jobs.recompute_source_reputation")
    importlib.import_module("merlin_worker.jobs.resolve_outcome")

    resolve_mod = sys.modules["merlin_worker.jobs.resolve_outcome"]
    recompute_mod = sys.modules["merlin_worker.jobs.recompute_source_reputation"]

    monkeypatch.setattr(
        "merlin_infrastructure.db.repositories.OutcomeRepository",
        lambda _s: outcome_repo,
    )
    monkeypatch.setattr(
        "merlin_infrastructure.db.session.get_session_factory",
        lambda _dsn: session_factory,
    )
    monkeypatch.setattr(
        "merlin_infrastructure.market_data.coingecko.CoinGeckoAdapter",
        lambda **_kw: market,
    )
    monkeypatch.setattr(
        "merlin_infrastructure.notify.telegram.notifier.TelegramNotifier",
        lambda **_kw: notifier,
    )

    # Stub the inline recompute. Resolver awaits this directly via a
    # local import inside ``_inline_recompute_with_async_fallback`` —
    # we monkeypatch on the recompute module so the local import
    # picks up the stub. Returns the fixture-pinned DEFAULT_INLINE_PNL
    # so happy-path tests can assert the value flows into the
    # notification body.
    async def _fake_recompute(source_id: str) -> float | None:
        inline_recompute_calls.append(source_id)
        return _ResolverFixture.DEFAULT_INLINE_PNL

    monkeypatch.setattr(recompute_mod, "_recompute_async", _fake_recompute)

    # Stub the async-fallback enqueue. Happy path MUST NOT touch this;
    # only the inline-recompute-failure path reaches for it. Tests
    # assert ``enqueued == []`` on happy paths.
    def _enqueue(*, source_id: str) -> None:
        enqueued.append(("recompute_source_reputation", {"source_id": source_id}))

    monkeypatch.setattr(resolve_mod, "_enqueue_reputation_recompute", _enqueue)

    return _ResolverFixture(
        outcome_repo=outcome_repo,
        market=market,
        notifier=notifier,
        session_factory=session_factory,
        enqueued=enqueued,
        inline_recompute_calls=inline_recompute_calls,
    )


def _seed_claim_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim_id: UUID,
    direction: str = "long",
    coingecko_id: str = "bitcoin",
    asset_symbol: str = "BTC",
) -> None:
    """Monkeypatch ``_ClaimContext.load`` to return a fixed context.

    The resolver's DB load is exercised separately in
    ``test_claim_context_load`` below; the dispatch tests focus on
    the post-load sequence.
    """
    # Same shadowing as the fixture — go through sys.modules.
    import sys

    import merlin_worker.jobs.resolve_outcome  # noqa: F401 — load the submodule

    resolve_mod = sys.modules["merlin_worker.jobs.resolve_outcome"]

    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    t_resolve = t0 + _dt.timedelta(hours=2)
    ctx = resolve_mod._ClaimContext(
        claim_id=claim_id,
        source_id="tg:" + "a" * 32,
        tg_user_id=42,
        coingecko_id=coingecko_id,
        asset_symbol=asset_symbol,
        direction=direction,
        horizon_seconds=7_200,
        t0=t0,
        t_resolve=t_resolve,
    )

    # ``classmethod`` wrappers receive ``cls`` as the first positional
    # argument when invoked through the class — our fake accepts (and
    # discards) it alongside the resolver's kw-only args.
    async def _fake_load(cls: Any, *, session: Any, claim_id: str) -> Any:
        del cls, session, claim_id
        return ctx

    monkeypatch.setattr(resolve_mod._ClaimContext, "load", classmethod(_fake_load))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path_persists_resolved_outcome_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """A long bet that moves +5% INSERTs a resolved row with ``is_correct=True``,
    triggers an inline reputation recompute, and sends the won template
    with the freshly-computed PnL threaded into the body.
    """
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)

    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=105.0, returned_at=t0 + _dt.timedelta(hours=2)),
    ]

    await _resolve_outcome_async(str(claim_id))

    # Exactly one resolved row was written.
    resolved_rows = [r for r in resolver_fixture.outcome_repo.rows if r["_kind"] == "resolved"]
    assert len(resolved_rows) == 1
    row = resolved_rows[0]
    assert row["claim_id"] == claim_id
    assert row["source_id"] == "tg:" + "a" * 32
    assert row["observed_return_pct"] == pytest.approx(5.0)
    assert row["is_correct"] is True
    assert row["price_t0_usd"] == pytest.approx(100.0)
    assert row["price_resolve_usd"] == pytest.approx(105.0)

    # Reputation recompute ran INLINE — exactly one call, for this source.
    assert resolver_fixture.inline_recompute_calls == ["tg:" + "a" * 32]
    # Happy path MUST NOT reach for the async-fallback enqueue.
    assert resolver_fixture.enqueued == []

    # User got the won template + the freshly-computed PnL +
    # /me · /board CTA.
    assert len(resolver_fixture.notifier.sent) == 1
    tg_id, text = resolver_fixture.notifier.sent[0]
    assert tg_id == 42
    assert "BTC" in text
    assert "called it right" in text
    assert "+5.0%" in text  # the bet's observed return
    # DEFAULT_INLINE_PNL = 6.13 → rendered as +6.1% (1 decimal place).
    assert "Your PnL is now +6.1%." in text
    assert "/me" in text
    assert "/board" in text


async def test_short_bet_market_up_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """A short bet on a market that moved up resolves as ``is_correct=False``
    and renders the lost template — still threads the fresh PnL +
    /me · /board CTA into the body."""
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id, direction="short")
    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=110.0, returned_at=t0 + _dt.timedelta(hours=2)),
    ]

    await _resolve_outcome_async(str(claim_id))

    resolved = resolver_fixture.outcome_repo.rows[0]
    assert resolved["is_correct"] is False
    _, text = resolver_fixture.notifier.sent[0]
    assert "the market went the other way" in text
    assert "Your PnL is now +6.1%." in text
    assert "/me" in text
    assert "/board" in text


async def test_idempotent_when_outcome_already_exists(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """Second invocation against an already-resolved claim is a no-op.

    RQ retries call the job twice; a corrective ``make admin-rerun``
    can also re-fire. Neither must double-notify the user, neither
    must trigger an inline recompute (the recompute already fired
    on the original invocation).
    """
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)
    # Pretend a prior run already INSERTed an outcome row.
    resolver_fixture.outcome_repo.existing_for_claim[claim_id] = type(
        "ExistingRecord",
        (),
        {"status": "resolved"},
    )()

    await _resolve_outcome_async(str(claim_id))

    # No new INSERT, no recompute, no fallback enqueue, no notification.
    assert resolver_fixture.outcome_repo.rows == []
    assert resolver_fixture.inline_recompute_calls == []
    assert resolver_fixture.enqueued == []
    assert resolver_fixture.notifier.sent == []


async def test_q023_asset_not_found_writes_rejection_row(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """``AssetNotFoundError`` from CoinGecko maps to a rejection outcome row.

    The reputation recompute MUST NOT fire — the source's hit rate
    is unaffected by an asset mutation, and the rejection template
    owns its own wording (no PnL chunk, no /me · /board CTA).
    """
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)
    resolver_fixture.market.raise_asset_not_found = True

    await _resolve_outcome_async(str(claim_id))

    rejected_rows = [
        r for r in resolver_fixture.outcome_repo.rows if r["_kind"] == "rejected_asset_mutation"
    ]
    assert len(rejected_rows) == 1
    assert rejected_rows[0]["claim_id"] == claim_id
    # Reputation untouched on both paths.
    assert resolver_fixture.inline_recompute_calls == []
    assert resolver_fixture.enqueued == []
    # User got the rejection template — no PnL chunk, no CTA.
    _, text = resolver_fixture.notifier.sent[0]
    assert "BTC" in text
    assert "listing changed" in text
    assert "Your PnL is now" not in text
    assert "/me" not in text
    assert "/board" not in text


def test_enqueue_reputation_recompute_targets_reputation_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reactive enqueue MUST pass ``REPUTATION_QUEUE_NAME`` to RQQueueAdapter.

    Pins the queue-mismatch regression at the function level rather
    than relying solely on the source-grep preflight.
    """
    from merlin_infrastructure.queue.names import REPUTATION_QUEUE_NAME
    from merlin_worker.jobs.resolve_outcome import _enqueue_reputation_recompute

    captured: dict[str, Any] = {}

    class _RecordingAdapter:
        def __init__(self, *, redis: Any, queue_name: str) -> None:
            captured["redis"] = redis
            captured["queue_name"] = queue_name

        def enqueue(self, fn_path: str, *args: Any, job_id: str | None = None) -> str:
            captured["fn_path"] = fn_path
            captured["args"] = args
            captured["job_id"] = job_id
            return "fake-job-id"

    class _StubRedis:
        @classmethod
        def from_url(cls, _url: str) -> _StubRedis:
            return cls()

    monkeypatch.setattr(
        "merlin_infrastructure.queue.rq.RQQueueAdapter",
        _RecordingAdapter,
    )
    monkeypatch.setattr("redis.Redis", _StubRedis)

    # Use a realistic source_id (``tg:<32 hex>``) — the previous test
    # value happened to omit characters that exercise the sanitiser.
    source_id = "tg:" + "a1b2c3d4" * 4
    _enqueue_reputation_recompute(source_id=source_id)

    assert captured["queue_name"] == REPUTATION_QUEUE_NAME
    assert captured["fn_path"] == "merlin_worker.jobs.recompute_source_reputation"
    # The job receives the unsanitised source_id as its argument — the
    # downstream repository keys off the canonical ``tg:`` form.
    assert captured["args"] == (source_id,)
    # The ``:`` is replaced with ``-`` so RQ's validate_job_id accepts
    # the result. The substitution is deterministic, so re-enqueues
    # naturally dedupe by job_id.
    assert captured["job_id"] == f"reputation-tg-{'a1b2c3d4' * 4}"
    assert ":" not in captured["job_id"]


async def test_reputation_failure_does_not_block_user_notification(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """Sprint-1 post-mortem invariant in its current shape:
    a reputation recompute failure MUST NOT drop the user notification.

    History: the original Sprint-1 regression had reputation enqueue
    BETWEEN the DB write and the notification. When the enqueue
    raised (bad job_id with a colon, the Sprint-1 follow-up bug),
    the notification was skipped, the row stayed in the DB, and the
    RQ retry hit the idempotency guard — leaving the user with no
    message. The fix moved enqueue to AFTER notify.

    The Sprint-1.5 architecture moved the recompute INLINE (we need
    its return value to thread PnL into the notification body), so
    "after notify" no longer applies. The protective invariant is
    re-pinned here at the right semantic level: the recompute path
    must be FULLY isolated by try/except, so an inline failure
    triggers the async-fallback enqueue AND the notification still
    goes out (just without the PnL chunk).
    """
    import sys

    import merlin_worker.jobs.recompute_source_reputation  # noqa: F401

    recompute_mod = sys.modules["merlin_worker.jobs.recompute_source_reputation"]
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)

    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=105.0, returned_at=t0 + _dt.timedelta(hours=2)),
    ]

    # Inline recompute raises — simulates a transient DB lock,
    # a hot-reload mid-import, anything.
    async def _boom(source_id: str) -> float | None:
        del source_id
        raise RuntimeError("DB had a moment")

    monkeypatch.setattr(recompute_mod, "_recompute_async", _boom)

    # Must not raise out of the resolver.
    await _resolve_outcome_async(str(claim_id))

    # Outcome row still persisted.
    assert len(resolver_fixture.outcome_repo.rows) == 1
    # Async-fallback enqueue fired so the snapshot row eventually lands.
    assert resolver_fixture.enqueued == [
        ("recompute_source_reputation", {"source_id": "tg:" + "a" * 32}),
    ]
    # User STILL got notified. Body has no PnL chunk (the fallback
    # path can't recover the recomputed value), but the CTA is there.
    assert len(resolver_fixture.notifier.sent) == 1
    _, text = resolver_fixture.notifier.sent[0]
    assert "BTC" in text
    assert "called it right" in text
    assert "Your PnL is now" not in text
    assert "/me" in text
    assert "/board" in text


async def test_async_fallback_enqueue_failure_does_not_crash_job(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """If BOTH the inline recompute and the async-fallback enqueue
    raise, the resolver still completes — the outcome row is
    persisted, the user is notified (without PnL chunk), and the
    leaderboard simply lags by one bet until the next outcome on
    this source triggers a fresh recompute.

    This is the failure-of-failures path: the inline recompute is
    transiently broken AND the queue is unreachable. Tolerable as a
    degraded mode; intolerable would be the resolver itself
    crashing out and the RQ retry then short-circuiting on the
    idempotency guard, leaving the user unnotified.
    """
    import sys

    import merlin_worker.jobs.recompute_source_reputation  # noqa: F401

    recompute_mod = sys.modules["merlin_worker.jobs.recompute_source_reputation"]
    resolve_mod = sys.modules["merlin_worker.jobs.resolve_outcome"]
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)
    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=105.0, returned_at=t0 + _dt.timedelta(hours=2)),
    ]

    async def _inline_boom(source_id: str) -> float | None:
        del source_id
        raise RuntimeError("inline recompute is broken")

    def _enqueue_boom(*, source_id: str) -> None:
        del source_id
        raise ValueError("job_id has a colon in it (Sprint-1 follow-up bug)")

    monkeypatch.setattr(recompute_mod, "_recompute_async", _inline_boom)
    monkeypatch.setattr(resolve_mod, "_enqueue_reputation_recompute", _enqueue_boom)

    # Must not raise even though BOTH reputation paths blew up.
    await _resolve_outcome_async(str(claim_id))

    # DB row + user notification still landed.
    assert len(resolver_fixture.outcome_repo.rows) == 1
    assert len(resolver_fixture.notifier.sent) == 1
    _, text = resolver_fixture.notifier.sent[0]
    assert "Your PnL is now" not in text  # no PnL — both paths failed
    assert "/me" in text  # CTA still renders
    assert "/board" in text


async def test_notification_failure_does_not_crash_job(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """A Telegram failure (user blocked the bot, transient 5xx, ...)
    after the DB row is written MUST NOT raise — the row is already
    persisted, so an RQ retry would just hit the idempotency guard."""
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)
    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=105.0, returned_at=t0 + _dt.timedelta(hours=2)),
    ]

    async def _boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("telegram is having a moment")

    resolver_fixture.notifier.notify_user = _boom  # type: ignore[method-assign]

    # Must not raise.
    await _resolve_outcome_async(str(claim_id))

    # DB row still persisted, inline recompute still ran successfully.
    assert len(resolver_fixture.outcome_repo.rows) == 1
    assert resolver_fixture.inline_recompute_calls == ["tg:" + "a" * 32]
    # No fallback enqueue (inline succeeded).
    assert resolver_fixture.enqueued == []


def test_reputation_job_id_helper_is_idempotent_and_colon_free() -> None:
    """The helper that builds reputation job_ids must produce an RQ-valid id
    against the real ``tg:<hex>`` source_id format. Pinning here so the
    unit-level guarantee matches the preflight grep guard.
    """
    from merlin_worker.jobs.resolve_outcome import _reputation_job_id

    out = _reputation_job_id("tg:" + "f" * 32)
    assert ":" not in out
    assert out == "reputation-tg-" + "f" * 32
    # Determinism — same input twice yields the same id.
    assert _reputation_job_id("tg:abc") == _reputation_job_id("tg:abc")


async def test_uses_historical_price_at_t_resolve_not_now(
    monkeypatch: pytest.MonkeyPatch,
    resolver_fixture: _ResolverFixture,
) -> None:
    """Worker fairness invariant: the resolver MUST ask CoinGecko for
    the price **at ``claim.resolves_at``**, never at ``datetime.utcnow()``.

    Why this is load-bearing: if the worker is down at the moment a
    bet expires (Redis backlog, restart, deploy, crash loop), it
    catches up later by draining the scheduled queue. Without this
    invariant a 2-hour bet that expires while the worker is down would
    be scored against whatever the price happens to be when the worker
    comes back — minutes, hours, or days later — instead of the price
    at the actual expiry instant the user committed to.

    CoinGecko's ``/market_chart/range`` is a historical endpoint with a
    ±5-minute bucket window, so the fix is naturally available; the
    pure test here pins that the resolver passes the right timestamp.
    """
    from merlin_worker.jobs.resolve_outcome import _resolve_outcome_async

    claim_id = uuid4()
    _seed_claim_context(monkeypatch, claim_id=claim_id)

    # Seed t0 + t_resolve from the seeded context (kept in sync with
    # ``_seed_claim_context``'s constants).
    t0 = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    t_resolve = t0 + _dt.timedelta(hours=2)
    resolver_fixture.market.prices["bitcoin"] = [
        _FakePrice(price_usd=100.0, returned_at=t0),
        _FakePrice(price_usd=101.0, returned_at=t_resolve),
    ]

    await _resolve_outcome_async(str(claim_id))

    # Two calls — first for t0, second for t_resolve. Order matters
    # for the (price_t0, price_resolve) tuple that feeds the scoring
    # delta. Neither call may be against ``datetime.now()``.
    assert resolver_fixture.market.requested_at == [t0, t_resolve]
