"""Unit tests for ``GET /me`` and ``PATCH /me``.

* ``PATCH /me`` — display-name finalisation. Sprint-1 wired this endpoint
  against the ``sources`` + ``source_identity`` tables so the onboarding
  flow actually completes end-to-end (display names survive a bot
  restart). The route looks the calling source up by ``tg_user_id`` and
  404s if the source hasn't been onboarded yet.

* ``GET /me`` — open-call snapshot. Resolves ``tg_user_id`` →
  ``source_id`` via ``source_identity``, then projects open + resolved
  counts. Returns an honest empty-state when the user has no resolved
  calls (``accuracy_pct`` is ``None``, NOT 0.0 — the contract makes
  this Optional precisely so the bot can render "tracking your first
  resolutions" instead of a misleading zero).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import Ports, get_ports, get_session, get_settings
from merlin_core.application.ports import (
    ClaimRecord,
    OutcomeHistoryEntry,
    ResolvedOutcomeForTicker,
    SourceRecord,
)
from merlin_infrastructure.notify.fake import FakeNotifier

TEST_TOKEN = "test-internal-token"


@dataclass
class _FakeSourceRepo:
    sources: dict[str, SourceRecord] = field(default_factory=dict)

    async def get_by_source_id(self, source_id: str) -> SourceRecord | None:
        return self.sources.get(source_id)

    async def update_display_name(self, source_id: str, display_name: str) -> SourceRecord:
        now = datetime.now(tz=UTC)
        existing = self.sources[source_id]
        updated = SourceRecord(
            source_id=existing.source_id,
            display_name=display_name,
            kind=existing.kind,
            reliability=existing.reliability,
            credibility=existing.credibility,
            consent_version=existing.consent_version,
            created_at=existing.created_at,
            updated_at=now,
        )
        self.sources[source_id] = updated
        return updated


@dataclass
class _FakeIdentityRepo:
    by_tg_id: dict[int, str] = field(default_factory=dict)

    async def get_source_id_by_tg_user_id(self, tg_user_id: int) -> str | None:
        return self.by_tg_id.get(tg_user_id)


@dataclass
class _FakeInviteRepo:
    """In-memory stand-in for ``InviteRepositoryPort``.

    The PATCH /me notification path only ever calls
    ``get_inviter_tg_user_id_by_redeemed_source_id``, so this fake
    skips the rest of the port surface. Tests seed
    ``inviter_tg_id_by_redeemer`` to control the lookup result —
    leaving the map empty exercises the "admin-bootstrapped user"
    branch (no invite row → no notification).
    """

    inviter_tg_id_by_redeemer: dict[str, int] = field(default_factory=dict)
    raises: Exception | None = None

    async def get_inviter_tg_user_id_by_redeemed_source_id(
        self,
        redeemed_by_source_id: str,
    ) -> int | None:
        if self.raises is not None:
            raise self.raises
        return self.inviter_tg_id_by_redeemer.get(redeemed_by_source_id)


@dataclass
class _FakeClaimRepo:
    open_by_source: dict[str, list[ClaimRecord]] = field(default_factory=dict)
    resolved_count_by_source: dict[str, int] = field(default_factory=dict)

    async def list_open_by_source(self, source_id: str) -> list[ClaimRecord]:
        return list(self.open_by_source.get(source_id, []))

    async def count_resolved_by_source(self, source_id: str) -> int:
        return self.resolved_count_by_source.get(source_id, 0)


@dataclass
class _FakeReputationRepo:
    """In-memory ``SourceReputationRepositoryPort`` stand-in.

    Tests seed ``snapshots_by_source`` to project a populated row, or
    leave the source out entirely to exercise the cold-start path
    (``latest_for_source`` returns ``None``).
    """

    snapshots_by_source: dict[str, Any] = field(default_factory=dict)

    async def latest_for_source(self, source_id: str) -> Any:
        return self.snapshots_by_source.get(source_id)


@dataclass
class _FakeOutcomeRepo:
    """In-memory stand-in for the ``OutcomeRepositoryPort`` reads.

    Backs the ``/me/tickers`` + ``/me/history`` routes. Tests seed:

    * ``ticker_rows_by_source`` — rows returned by
      ``fetch_resolved_for_tickers`` (drives the ticker breakdown).
    * ``history_rows_by_source`` — newest-first rows returned by
      ``fetch_history_page``. The fake honours the route's
      ``limit`` / ``offset`` / ``as_of`` so pagination boundary
      tests don't need a separate slicer. ``as_of`` filters rows by
      ``resolved_at <= as_of`` to mirror the SQL WHERE clause.
    * ``last_fetch_history_kwargs`` — captures the kwargs the route
      passed on the most recent ``fetch_history_page`` call, so
      tests can pin the contract (as_of round-trip, etc).

    The mutation methods (``insert_resolved`` etc.) are unused on
    these routes and stay absent — the route only invokes the read
    methods.
    """

    ticker_rows_by_source: dict[str, list[ResolvedOutcomeForTicker]] = field(default_factory=dict)
    history_rows_by_source: dict[str, list[OutcomeHistoryEntry]] = field(default_factory=dict)
    last_fetch_history_kwargs: dict[str, Any] = field(default_factory=dict)

    async def fetch_resolved_for_tickers(
        self,
        source_id: str,
    ) -> list[ResolvedOutcomeForTicker]:
        return list(self.ticker_rows_by_source.get(source_id, []))

    async def fetch_history_page(
        self,
        source_id: str,
        *,
        as_of: datetime,
        limit: int,
        offset: int,
    ) -> tuple[list[OutcomeHistoryEntry], int]:
        self.last_fetch_history_kwargs = {
            "source_id": source_id,
            "as_of": as_of,
            "limit": limit,
            "offset": offset,
        }
        rows = self.history_rows_by_source.get(source_id, [])
        # Mirror the SQL ``WHERE resolved_at <= :as_of`` filter so
        # tests can pin the frozen-window behaviour against the fake.
        windowed = [r for r in rows if r.resolved_at <= as_of]
        page = list(windowed[offset : offset + limit])
        return page, len(windowed)


@dataclass
class _FakeSession:
    sources: _FakeSourceRepo = field(default_factory=_FakeSourceRepo)
    identities: _FakeIdentityRepo = field(default_factory=_FakeIdentityRepo)
    invites: _FakeInviteRepo = field(default_factory=_FakeInviteRepo)
    claims: _FakeClaimRepo = field(default_factory=_FakeClaimRepo)
    reputation: _FakeReputationRepo = field(default_factory=_FakeReputationRepo)
    outcomes: _FakeOutcomeRepo = field(default_factory=_FakeOutcomeRepo)
    committed: bool = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None: ...

    async def get(self, *_args: Any, **_kw: Any) -> None:
        return None


@pytest.fixture
def fake_session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    """Captures every PATCH /me invitee-handshake notification."""
    return FakeNotifier()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        merlin_api_internal_token=TEST_TOKEN,
        sentry_dsn="",
    )


@pytest.fixture
async def client(
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    from merlin_infrastructure.db import repositories as _repos_mod

    monkeypatch.setattr(_repos_mod, "SourceRepository", lambda _s: fake_session.sources)
    monkeypatch.setattr(
        _repos_mod,
        "SourceIdentityRepository",
        lambda _s: fake_session.identities,
    )
    monkeypatch.setattr(_repos_mod, "InviteRepository", lambda _s: fake_session.invites)
    monkeypatch.setattr(_repos_mod, "ClaimRepository", lambda _s: fake_session.claims)
    monkeypatch.setattr(
        _repos_mod,
        "SourceReputationRepository",
        lambda _s: fake_session.reputation,
    )
    monkeypatch.setattr(_repos_mod, "OutcomeRepository", lambda _s: fake_session.outcomes)

    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield fake_session

    def _fake_get_ports() -> Ports:
        # PATCH /me only reaches for ``ports.notifier``; the rest of
        # the Ports surface is unused on this route, so we stub the
        # other fields with ``None`` typed as ``Any`` rather than
        # construct real adapters in a unit test.
        return Ports(
            drafter=None,  # type: ignore[arg-type]
            market_data=None,  # type: ignore[arg-type]
            queue=None,  # type: ignore[arg-type]
            notifier=fake_notifier,
        )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_ports] = _fake_get_ports
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _seed_onboarded(session: _FakeSession, *, tg_user_id: int, source_id: str) -> None:
    """Convenience: seed a fully-onboarded user with no claims yet."""
    session.identities.by_tg_id[tg_user_id] = source_id
    session.sources.sources[source_id] = SourceRecord(
        source_id=source_id,
        display_name="Yuri",
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _claim(*, claim_id: UUID, source_id: str, **overrides: Any) -> ClaimRecord:
    """Build a ClaimRecord with sensible defaults.

    ``price_usd_at_submit`` defaults to ``None`` to match the
    real-world shape of a row submitted while CoinGecko was
    unreachable (or a legacy row predating the column). Tests
    that pin the price-propagation contract override it via
    ``**overrides``.
    """
    defaults: dict[str, Any] = {
        "id": claim_id,
        "source_id": source_id,
        "asset_coingecko_id": "ethereum",
        "asset_symbol": "ETH",
        "asset_name": "Ethereum",
        "direction": "long",
        "horizon_seconds": 1_209_600,
        "conviction": "strong",
        "thesis_text": "Vitalik staking news",
        "asset_query": "ETH",
        "submitted_at": datetime.now(tz=UTC),
        "resolves_at": datetime.now(tz=UTC) + timedelta(days=14),
        "parsed_via": "freeform_llm",
        "price_usd_at_submit": None,
        "extracted_fields": {},
        "parse_metadata": {},
    }
    defaults.update(overrides)
    return ClaimRecord(**defaults)


async def test_patch_me_persists_display_name(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Seed an onboarded source, PATCH /me, assert the row was updated."""
    src_id = "tg:0000111122223333abcdabcd00001111"
    fake_session.identities.by_tg_id[42] = src_id
    fake_session.sources.sources[src_id] = SourceRecord(
        source_id=src_id,
        display_name=None,
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    response = await client.patch(
        "/me",
        json={"display_name": "Alice", "tg_user_id": 42},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    assert fake_session.sources.sources[src_id].display_name == "Alice"
    assert fake_session.committed


async def test_patch_me_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A user who hasn't been onboarded yet can't set a display name —
    the route MUST 404 rather than silently doing nothing."""
    response = await client.patch(
        "/me",
        json={"display_name": "Ghost", "tg_user_id": 9999},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert not fake_session.committed


# ---------------------------------------------------------------------------
# PATCH /me — inviter handshake notification.
#
# The first-ever display-name set after an invite redemption fires a
# one-shot "🤝🏻 X has joined using your invite code!" notification to
# the inviter. Pin the contract from four angles: it fires on the
# first-time set, it does NOT fire on a later rename, it does NOT
# fire for admin-bootstrapped users, and a notifier failure does not
# break the user's onboarding.
# ---------------------------------------------------------------------------


def _seed_invitee_with_inviter(
    session: _FakeSession,
    *,
    invitee_tg_id: int,
    invitee_source_id: str,
    inviter_tg_id: int,
    display_name: str | None,
) -> None:
    """Onboarded invitee + a seeded invite row pointing at the inviter."""
    session.identities.by_tg_id[invitee_tg_id] = invitee_source_id
    session.sources.sources[invitee_source_id] = SourceRecord(
        source_id=invitee_source_id,
        display_name=display_name,
        kind="user",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.invites.inviter_tg_id_by_redeemer[invitee_source_id] = inviter_tg_id


async def test_patch_me_first_time_name_set_notifies_inviter(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
) -> None:
    """The invite-joined happy path the user requested.

    Invitee has ``display_name=None`` (the bot's redeem step lands the
    row in that state). PATCH /me with the first display name must
    commit the row AND deliver the handshake message to the inviter's
    raw TG id, verbatim from the template.
    """
    invitee_src_id = "tg:" + "a" * 32
    _seed_invitee_with_inviter(
        fake_session,
        invitee_tg_id=42,
        invitee_source_id=invitee_src_id,
        inviter_tg_id=999_000_111,
        display_name=None,
    )

    response = await client.patch(
        "/me",
        json={"display_name": "Bob", "tg_user_id": 42},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    assert fake_session.committed
    assert fake_session.sources.sources[invitee_src_id].display_name == "Bob"

    assert len(fake_notifier.calls) == 1, (
        "PATCH /me must deliver exactly one inviter handshake notification "
        "on the first-time display-name set after invite redemption"
    )
    call = fake_notifier.calls[0]
    assert call.tg_user_id == 999_000_111
    # Pin the literal user-visible string so a future template tweak
    # that drifts the wording trips this test before the inviter ever
    # sees the change.
    assert call.text == "🤝🏻 Bob has joined using your invite code!"


async def test_patch_me_rename_does_not_renotify_inviter(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
) -> None:
    """Second PATCH /me (rename) MUST be silent.

    The inviter should see exactly one handshake message per invitee,
    on the first-ever name set. If the invitee later renames
    themselves the inviter doesn't need to know — re-firing the
    handshake would read as spam.
    """
    invitee_src_id = "tg:" + "b" * 32
    # Seed the invitee with a name already set (i.e. they're past the
    # first-time transition — this is what a rename PATCH looks like).
    _seed_invitee_with_inviter(
        fake_session,
        invitee_tg_id=43,
        invitee_source_id=invitee_src_id,
        inviter_tg_id=999_000_222,
        display_name="Bob",
    )

    response = await client.patch(
        "/me",
        json={"display_name": "Bob the Builder", "tg_user_id": 43},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    assert fake_session.sources.sources[invitee_src_id].display_name == "Bob the Builder"
    assert fake_notifier.calls == [], (
        "PATCH /me on an already-named source must not re-notify the inviter — "
        "the handshake is a one-shot keyed on the first-time name set"
    )


async def test_patch_me_first_time_set_without_invite_does_not_notify(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
) -> None:
    """Admin-bootstrapped user (no invite row) → no notification target.

    ``admin_bootstrap`` lands a source row without going through
    ``POST /invites/redeem``, so the invite-lookup returns ``None`` and
    PATCH /me must complete silently. The user is still onboarded; the
    inviter handshake is simply not applicable to this path.
    """
    src_id = "tg:" + "c" * 32
    fake_session.identities.by_tg_id[44] = src_id
    fake_session.sources.sources[src_id] = SourceRecord(
        source_id=src_id,
        display_name=None,
        kind="admin",
        reliability="F",
        credibility=6,
        consent_version="v0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    # NOTE: no entry in fake_session.invites — the lookup returns None.

    response = await client.patch(
        "/me",
        json={"display_name": "Operator", "tg_user_id": 44},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    assert fake_session.sources.sources[src_id].display_name == "Operator"
    assert fake_notifier.calls == [], (
        "An admin-bootstrapped user (no invite row) has no inviter to "
        "notify — PATCH /me must complete without any notification"
    )


async def test_patch_me_inviter_lookup_failure_does_not_break_onboarding(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
) -> None:
    """A failing inviter-lookup must not roll back the user's name change.

    The row is already COMMITted by the time we try to look up + notify
    the inviter. A repository error there must surface as a swallowed
    log line, NOT a 5xx that makes the bot's ``receive_display_name``
    retry. Without this guard, a transient DB blip on the lookup query
    would block every new invitee from finishing onboarding.
    """
    invitee_src_id = "tg:" + "d" * 32
    _seed_invitee_with_inviter(
        fake_session,
        invitee_tg_id=45,
        invitee_source_id=invitee_src_id,
        inviter_tg_id=999_000_333,
        display_name=None,
    )
    fake_session.invites.raises = RuntimeError("simulated DB blip")

    response = await client.patch(
        "/me",
        json={"display_name": "Bob", "tg_user_id": 45},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    # Row is committed even though the inviter lookup blew up.
    assert fake_session.committed
    assert fake_session.sources.sources[invitee_src_id].display_name == "Bob"
    # And the notifier was never reached.
    assert fake_notifier.calls == []


async def test_patch_me_notifier_failure_does_not_break_onboarding(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    fake_notifier: FakeNotifier,
) -> None:
    """A failing notifier must not roll back the user's name change.

    The row is already COMMITted by the time we try to deliver the
    handshake. A transport error from Telegram (timeout, 5xx, JSON
    encoding glitch) must surface as a swallowed log line, NOT a 5xx
    that makes the bot's ``receive_display_name`` retry. Without this
    guard, a transient Telegram outage would block every new invitee
    from finishing onboarding.

    Pins the OUTER ``try/except`` around ``notifier.notify_user`` — the
    inner ``try/except`` around the inviter lookup is covered by
    ``test_patch_me_inviter_lookup_failure_does_not_break_onboarding``;
    both branches need explicit coverage because they protect against
    different upstream failures (DB blip vs. Telegram blip).
    """
    invitee_src_id = "tg:" + "f" * 32
    _seed_invitee_with_inviter(
        fake_session,
        invitee_tg_id=46,
        invitee_source_id=invitee_src_id,
        inviter_tg_id=999_000_444,
        display_name=None,
    )
    fake_notifier.raise_on_call = RuntimeError("simulated telegram outage")

    response = await client.patch(
        "/me",
        json={"display_name": "Bob", "tg_user_id": 46},
        headers=auth_headers,
    )
    assert response.status_code == 204, response.text
    # Row is committed even though the notifier blew up.
    assert fake_session.committed
    assert fake_session.sources.sources[invitee_src_id].display_name == "Bob"
    # The route did REACH the notifier — the call landed in `calls`
    # before the fake re-raised — but the exception was caught.
    assert len(fake_notifier.calls) == 1
    assert fake_notifier.calls[0].tg_user_id == 999_000_444


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


async def test_get_me_returns_open_calls_and_display_name(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Happy path: onboarded user with one open call — the route must
    project the source's display name + the open call's wire-format
    fields."""
    src_id = "tg:0000111122223333abcdabcd00001111"
    _seed_onboarded(fake_session, tg_user_id=42, source_id=src_id)
    claim_id = uuid4()
    fake_session.claims.open_by_source[src_id] = [
        _claim(claim_id=claim_id, source_id=src_id),
    ]
    fake_session.claims.resolved_count_by_source[src_id] = 0

    response = await client.get("/me", params={"tg_user_id": 42}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == "Yuri"
    assert len(body["calls_open"]) == 1
    open_call = body["calls_open"][0]
    assert open_call["asset_symbol"] == "ETH"
    assert open_call["direction"] == "long"
    assert open_call["horizon_seconds"] == 1_209_600
    assert UUID(open_call["claim_id"]) == claim_id
    assert body["calls_resolved_total"] == 0
    # Honest empty-state — never 0.0% accuracy when nothing has resolved.
    assert body["accuracy_pct"] is None
    assert body["rank"] is None


async def test_get_me_open_call_propagates_entry_price(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """``OpenCallEntry.entry_price_usd`` must carry through from the
    persisted ``ClaimRecord.price_usd_at_submit`` column to the wire
    JSON. Without this, the bot's /me renderer has nothing to show
    even when the API captured the snapshot at submit time.

    Live-bug class: a missed field in the route's projection means
    "did the column get filled?" and "does the user see it?" can
    drift independently. This test pins they don't.
    """
    src_id = "tg:0000111122223333abcdabcd00001111"
    _seed_onboarded(fake_session, tg_user_id=42, source_id=src_id)
    claim_id = uuid4()
    fake_session.claims.open_by_source[src_id] = [
        _claim(
            claim_id=claim_id,
            source_id=src_id,
            price_usd_at_submit=2438.12,
        ),
    ]

    response = await client.get("/me", params={"tg_user_id": 42}, headers=auth_headers)
    assert response.status_code == 200, response.text
    open_call = response.json()["calls_open"][0]
    assert open_call["entry_price_usd"] == pytest.approx(2438.12), (
        "GET /me did not surface entry_price_usd from the claim row — "
        "the bot's /me list will silently drop the entry-price chunk "
        "for every claim, including ones the API captured at submit."
    )


async def test_get_me_open_call_entry_price_serialises_null_when_absent(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A claim row with NULL ``price_usd_at_submit`` (legacy / CoinGecko
    down at submit) must serialise as ``"entry_price_usd": null`` —
    NOT a missing key, NOT a 0.0 placeholder. The bot's renderer
    distinguishes "no snapshot" from "captured zero" and would render
    "$0.00" if we lied about absence.
    """
    src_id = "tg:" + "b" * 32
    _seed_onboarded(fake_session, tg_user_id=43, source_id=src_id)
    fake_session.claims.open_by_source[src_id] = [
        _claim(
            claim_id=uuid4(),
            source_id=src_id,
            price_usd_at_submit=None,
        ),
    ]

    response = await client.get("/me", params={"tg_user_id": 43}, headers=auth_headers)
    assert response.status_code == 200, response.text
    open_call = response.json()["calls_open"][0]
    # The field MUST be present in the payload (so bot dicts can look
    # it up without a KeyError) and explicitly null (so the renderer
    # treats it as "no snapshot").
    assert "entry_price_usd" in open_call
    assert open_call["entry_price_usd"] is None


async def test_get_me_empty_state(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Onboarded user with zero claims — the route returns an empty
    list (NOT a placeholder), and accuracy/rank stay null. The bot
    renders the "tracking your first resolutions" copy from these
    nulls."""
    src_id = "tg:" + "z" * 32
    _seed_onboarded(fake_session, tg_user_id=7, source_id=src_id)

    response = await client.get("/me", params={"tg_user_id": 7}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["calls_open"] == []
    assert body["calls_resolved_recent"] == []
    assert body["calls_resolved_total"] == 0
    assert body["accuracy_pct"] is None
    assert body["rank"] is None


async def test_get_me_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A user who hasn't been onboarded gets a 404 (the bot reads
    this and points them at /start). Critical: we MUST NOT return an
    empty MeResponse here — that would tell unboarded users they have
    "no open calls" rather than telling them they aren't signed up."""
    response = await client.get("/me", params={"tg_user_id": 12345}, headers=auth_headers)
    assert response.status_code == 404


async def test_get_me_requires_tg_user_id_param(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Without ``tg_user_id`` the API has no way to identify the
    caller — must 422, never silently default to a magic source."""
    response = await client.get("/me", headers=auth_headers)
    assert response.status_code == 422


async def test_get_me_uses_reputation_snapshot_when_available(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """When ``source_reputation_history`` has a row, ``/me`` projects
    its denormalised counts + accuracy_pct.

    Pins the contract that the recompute job's output reaches the bot.
    Without this, the route could silently keep returning ``accuracy_pct
    is None`` even after outcomes start resolving — the user would
    never see their hit rate.
    """
    from merlin_core.application.ports import SourceReputationRecord

    src_id = "tg:" + "c" * 32
    _seed_onboarded(fake_session, tg_user_id=99, source_id=src_id)
    fake_session.reputation.snapshots_by_source[src_id] = SourceReputationRecord(
        id=uuid4(),
        source_id=src_id,
        outcomes_resolved=4,
        outcomes_correct=3,
        accuracy_pct=75.0,
        weighted_pnl_pct=2.5,
        sort_score=1.111,
        triggering_outcome_id=None,
        effective_at=datetime.now(tz=UTC),
    )

    response = await client.get("/me", params={"tg_user_id": 99}, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["calls_resolved_total"] == 4
    assert body["accuracy_pct"] == pytest.approx(75.0)
    # ``weighted_pnl_pct`` is the bot's primary headline — must project
    # through to the wire from the latest snapshot.
    assert body["weighted_pnl_pct"] == pytest.approx(2.5)


async def test_get_me_cold_start_omits_pnl_too(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """No reputation snapshot → both ``accuracy_pct`` AND
    ``weighted_pnl_pct`` are ``None`` (not 0.0). The bot renders the
    "tracking your first resolutions" empty-state instead of a
    misleading +0.0% PnL."""
    src_id = "tg:" + "e" * 32
    _seed_onboarded(fake_session, tg_user_id=101, source_id=src_id)
    # No reputation snapshot seeded.

    response = await client.get("/me", params={"tg_user_id": 101}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["accuracy_pct"] is None
    assert body["weighted_pnl_pct"] is None


async def test_get_me_cold_start_falls_back_to_horizon_count(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """No reputation snapshot yet → ``calls_resolved_total`` falls
    back to the ``count_resolved_by_source`` proxy.

    Mid-recompute users (claim resolved on the wall clock but the
    worker hasn't INSERTed the snapshot yet) still see an honest
    count rather than 0. ``accuracy_pct`` stays ``None`` because we
    only have proxy data, not a real hit-rate calculation.
    """
    src_id = "tg:" + "d" * 32
    _seed_onboarded(fake_session, tg_user_id=100, source_id=src_id)
    fake_session.claims.resolved_count_by_source[src_id] = 2
    # No reputation snapshot seeded.

    response = await client.get("/me", params={"tg_user_id": 100}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["calls_resolved_total"] == 2
    assert body["accuracy_pct"] is None


# ---------------------------------------------------------------------------
# GET /me/tickers
# ---------------------------------------------------------------------------


def _ticker_row(
    *,
    asset_symbol: str,
    direction: str = "long",
    conviction: str = "medium",
    observed_return_pct: float = 1.0,
    is_correct: bool = True,
) -> ResolvedOutcomeForTicker:
    """Builder for ``/me/tickers`` test inputs (per-bet resolved row)."""
    return ResolvedOutcomeForTicker(
        direction=direction,
        conviction=conviction,
        observed_return_pct=observed_return_pct,
        is_correct=is_correct,
        asset_symbol=asset_symbol,
    )


async def test_get_my_tickers_returns_breakdown_for_seeded_outcomes(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Two tickers seeded → two entries returned with the correct
    per-ticker numbers. Pins the wire shape + the projection from the
    use case's ``TickerStats`` to the Pydantic ``TickerEntry``."""
    src_id = "tg:" + "a" * 32
    _seed_onboarded(fake_session, tg_user_id=42, source_id=src_id)
    fake_session.outcomes.ticker_rows_by_source[src_id] = [
        _ticker_row(
            asset_symbol="TON",
            direction="short",
            conviction="strong",
            observed_return_pct=-7.61,
            is_correct=True,
        ),
        _ticker_row(
            asset_symbol="BTC",
            direction="long",
            conviction="weak",
            observed_return_pct=-1.32,
            is_correct=False,
        ),
    ]

    response = await client.get(
        "/me/tickers",
        params={"tg_user_id": 42},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    entries = body["entries"]
    assert len(entries) == 2
    by_symbol = {e["asset_symbol"]: e for e in entries}
    assert set(by_symbol) == {"TON", "BTC"}
    assert by_symbol["TON"]["calls_resolved"] == 1
    assert by_symbol["TON"]["calls_correct"] == 1
    assert by_symbol["TON"]["accuracy_pct"] == pytest.approx(100.0)
    assert by_symbol["TON"]["weighted_pnl_pct"] == pytest.approx(7.61, abs=1e-3)
    assert by_symbol["BTC"]["calls_resolved"] == 1
    assert by_symbol["BTC"]["calls_correct"] == 0


async def test_get_my_tickers_empty_for_cold_start_source(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A source with no resolved outcomes returns ``entries: []`` —
    the bot renders that as the cold-start empty-state copy."""
    src_id = "tg:" + "b" * 32
    _seed_onboarded(fake_session, tg_user_id=43, source_id=src_id)
    # No outcome rows seeded.

    response = await client.get(
        "/me/tickers",
        params={"tg_user_id": 43},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"entries": []}


async def test_get_my_tickers_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """An un-onboarded user gets a 404 — the bot reads this and
    points them at /start."""
    response = await client.get(
        "/me/tickers",
        params={"tg_user_id": 99_999},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_get_my_tickers_requires_tg_user_id(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Without ``tg_user_id`` the API can't identify the caller — 422."""
    response = await client.get("/me/tickers", headers=auth_headers)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /me/history
# ---------------------------------------------------------------------------


def _history_row(
    *,
    asset_symbol: str = "ETH",
    direction: str = "long",
    horizon_seconds: int = 86_400,
    observed_return_pct: float = 2.0,
    is_correct: bool = True,
    days_ago: int = 0,
) -> OutcomeHistoryEntry:
    """Builder for ``/me/history`` test inputs.

    Note: ``resolved_at`` is shifted a few seconds into the past on
    top of ``days_ago``. The server truncates the ``as_of`` ceiling
    to whole-second precision (so the bot's epoch-encoded callback
    round-trips exactly); fixtures with ``days_ago=0`` would land in
    the same second as the request and get filtered out by
    ``resolved_at <= as_of``. The extra padding keeps the fixture
    stable without faking the wall clock.
    """
    return OutcomeHistoryEntry(
        claim_id=uuid4(),
        asset_symbol=asset_symbol,
        direction=direction,
        horizon_seconds=horizon_seconds,
        observed_return_pct=observed_return_pct,
        is_correct=is_correct,
        resolved_at=(
            datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=days_ago, seconds=5)
        ),
    )


async def test_get_my_history_returns_first_page_with_has_more_true(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Five resolved outcomes, page size 2 → first page returns two
    rows, ``has_more=True``, ``next_offset=2``. Pins the pagination
    breadcrumbs the bot's inline button relies on."""
    src_id = "tg:" + "c" * 32
    _seed_onboarded(fake_session, tg_user_id=44, source_id=src_id)
    rows = [
        _history_row(asset_symbol="TON", days_ago=0),
        _history_row(asset_symbol="BTC", days_ago=1),
        _history_row(asset_symbol="ETH", days_ago=2),
        _history_row(asset_symbol="SOL", days_ago=3),
        _history_row(asset_symbol="ARB", days_ago=4),
    ]
    fake_session.outcomes.history_rows_by_source[src_id] = rows

    response = await client.get(
        "/me/history",
        params={"tg_user_id": 44, "limit": 2, "offset": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["entries"]) == 2
    assert body["entries"][0]["asset_symbol"] == "TON"
    assert body["entries"][1]["asset_symbol"] == "BTC"
    assert body["has_more"] is True
    assert body["next_offset"] == 2
    # The server stamped an ``as_of`` we can round-trip.
    assert isinstance(body["as_of"], str) and body["as_of"]


async def test_get_my_history_last_page_flips_has_more_false(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """The page that lands at the end of the list must report
    ``has_more=False`` and ``next_offset=None``. Without this, the
    bot would render a "Show 30 more" button that returns an empty
    page on click."""
    src_id = "tg:" + "d" * 32
    _seed_onboarded(fake_session, tg_user_id=45, source_id=src_id)
    rows = [
        _history_row(asset_symbol="TON", days_ago=0),
        _history_row(asset_symbol="BTC", days_ago=1),
        _history_row(asset_symbol="ETH", days_ago=2),
    ]
    fake_session.outcomes.history_rows_by_source[src_id] = rows

    response = await client.get(
        "/me/history",
        params={"tg_user_id": 45, "limit": 2, "offset": 2},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["asset_symbol"] == "ETH"
    assert body["has_more"] is False
    assert body["next_offset"] is None


async def test_get_my_history_empty_for_cold_start_source(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """No resolved outcomes → empty entries + no pagination breadcrumbs.

    The bot renders this as the cold-start "no resolved calls yet"
    copy.
    """
    src_id = "tg:" + "e" * 32
    _seed_onboarded(fake_session, tg_user_id=46, source_id=src_id)

    response = await client.get(
        "/me/history",
        params={"tg_user_id": 46},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["has_more"] is False
    assert body["next_offset"] is None
    # ``as_of`` is stamped even on the cold-start path so the bot can
    # round-trip it on a (no-op) refresh without crashing.
    assert isinstance(body["as_of"], str) and body["as_of"]


async def test_get_my_history_computes_is_flat_server_side(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A +0.3% return is below the 0.5% flat threshold and must come
    out as ``is_flat=True`` even though ``is_correct=False`` (flats
    are wash, not correct). The bot reads this bool to pick the 📝
    emoji — keeping the threshold server-side is what stops the bot
    from needing to import ``merlin_core.domain.scoring``.
    """
    src_id = "tg:" + "f" * 32
    _seed_onboarded(fake_session, tg_user_id=47, source_id=src_id)
    flat_row = _history_row(
        asset_symbol="ETH",
        direction="long",
        observed_return_pct=0.3,
        is_correct=False,
    )
    lost_row = _history_row(
        asset_symbol="BTC",
        direction="long",
        observed_return_pct=-3.0,
        is_correct=False,
        days_ago=1,
    )
    won_row = _history_row(
        asset_symbol="TON",
        direction="short",
        observed_return_pct=-5.0,
        is_correct=True,
        days_ago=2,
    )
    fake_session.outcomes.history_rows_by_source[src_id] = [flat_row, lost_row, won_row]

    response = await client.get(
        "/me/history",
        params={"tg_user_id": 47},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    by_symbol = {e["asset_symbol"]: e for e in entries}
    # Flat row: |0.3| < 0.5 threshold → is_flat True, is_correct False.
    assert by_symbol["ETH"]["is_flat"] is True
    assert by_symbol["ETH"]["is_correct"] is False
    # Lost row: |-3.0| > 0.5 → not flat; long lost → not correct.
    assert by_symbol["BTC"]["is_flat"] is False
    assert by_symbol["BTC"]["is_correct"] is False
    # Won row: |-5.0| > 0.5 → not flat; short paid → correct.
    assert by_symbol["TON"]["is_flat"] is False
    assert by_symbol["TON"]["is_correct"] is True


async def test_get_my_history_first_page_stamps_as_of_with_now(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """When the caller omits ``as_of`` (first ``/history`` invocation),
    the server stamps it with ``datetime.now(UTC)`` and passes the
    same value to the repository. Pins the contract: the first page's
    frozen window is the request's wall clock."""
    src_id = "tg:" + "j" * 32
    _seed_onboarded(fake_session, tg_user_id=51, source_id=src_id)
    fake_session.outcomes.history_rows_by_source[src_id] = [
        _history_row(asset_symbol="TON", days_ago=0),
    ]

    before = datetime.now(tz=UTC)
    response = await client.get(
        "/me/history",
        params={"tg_user_id": 51},
        headers=auth_headers,
    )
    after = datetime.now(tz=UTC)

    assert response.status_code == 200, response.text
    body = response.json()
    server_as_of = datetime.fromisoformat(body["as_of"].replace("Z", "+00:00"))
    # Server truncates ``as_of`` to second precision so the bot's
    # integer-epoch callback_data round-trips exactly (see
    # ``_normalise_as_of``). Compare on the same precision boundary.
    assert server_as_of.microsecond == 0
    assert before.replace(microsecond=0) <= server_as_of
    assert server_as_of <= after.replace(microsecond=0)

    repo_as_of = fake_session.outcomes.last_fetch_history_kwargs["as_of"]
    assert repo_as_of == server_as_of


async def test_get_my_history_round_trips_as_of_verbatim(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Subsequent pages must round-trip the caller's ``as_of`` value
    verbatim — the server passes it through to the repository so the
    frozen-window filter holds across the session."""
    src_id = "tg:" + "k" * 32
    _seed_onboarded(fake_session, tg_user_id=52, source_id=src_id)
    fake_session.outcomes.history_rows_by_source[src_id] = [
        _history_row(asset_symbol="TON", days_ago=0),
    ]

    pinned = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    response = await client.get(
        "/me/history",
        params={
            "tg_user_id": 52,
            "as_of": pinned.isoformat().replace("+00:00", "Z"),
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    server_echo = datetime.fromisoformat(body["as_of"].replace("Z", "+00:00"))
    assert server_echo == pinned

    repo_as_of = fake_session.outcomes.last_fetch_history_kwargs["as_of"]
    assert repo_as_of == pinned


async def test_get_my_history_truncates_caller_supplied_microseconds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A caller-supplied ``as_of`` with sub-second precision must be
    truncated to whole seconds before it hits the SQL filter — and
    the response must echo the truncated value. The bot's
    ``callback_data`` encodes ``as_of`` as an integer epoch, so any
    sub-second component the server preserves would cause the bot's
    follow-up request to land on a *different* ``as_of`` than the
    page-1 stamp, drifting pagination by a sub-second window.
    """
    src_id = "tg:" + "n" * 32
    _seed_onboarded(fake_session, tg_user_id=54, source_id=src_id)

    pinned_with_micros = datetime(2026, 4, 28, 12, 0, 0, 789_012, tzinfo=UTC)
    pinned_truncated = pinned_with_micros.replace(microsecond=0)

    response = await client.get(
        "/me/history",
        params={
            "tg_user_id": 54,
            "as_of": pinned_with_micros.isoformat().replace("+00:00", "Z"),
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    server_echo = datetime.fromisoformat(body["as_of"].replace("Z", "+00:00"))
    assert server_echo.microsecond == 0
    assert server_echo == pinned_truncated

    repo_as_of = fake_session.outcomes.last_fetch_history_kwargs["as_of"]
    assert repo_as_of == pinned_truncated


async def test_get_my_history_as_of_freezes_window_against_newer_rows(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """A row that resolved AFTER the caller's ``as_of`` ceiling must
    NOT appear in the page. This is the core pagination-drift fix:
    if a new outcome lands between page 1 and the "Show 30 more"
    click, it must not shift already-paged rows down (which would
    dup the boundary row).
    """
    src_id = "tg:" + "m" * 32
    _seed_onboarded(fake_session, tg_user_id=53, source_id=src_id)

    pinned = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    after_pin = pinned + timedelta(minutes=5)
    before_pin = pinned - timedelta(minutes=5)

    new_row = OutcomeHistoryEntry(
        claim_id=uuid4(),
        asset_symbol="NEW",
        direction="long",
        horizon_seconds=86_400,
        observed_return_pct=10.0,
        is_correct=True,
        resolved_at=after_pin,
    )
    old_row = OutcomeHistoryEntry(
        claim_id=uuid4(),
        asset_symbol="OLD",
        direction="long",
        horizon_seconds=86_400,
        observed_return_pct=2.0,
        is_correct=True,
        resolved_at=before_pin,
    )
    fake_session.outcomes.history_rows_by_source[src_id] = [new_row, old_row]

    response = await client.get(
        "/me/history",
        params={
            "tg_user_id": 53,
            "as_of": pinned.isoformat().replace("+00:00", "Z"),
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    symbols = [e["asset_symbol"] for e in entries]
    assert "NEW" not in symbols, "new outcome past as_of must not appear in the frozen page"
    assert symbols == ["OLD"]


async def test_get_my_history_validates_limit_bounds(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """``limit`` must land in [1, 100]; out-of-range → 422."""
    src_id = "tg:" + "g" * 32
    _seed_onboarded(fake_session, tg_user_id=48, source_id=src_id)

    too_small = await client.get(
        "/me/history",
        params={"tg_user_id": 48, "limit": 0},
        headers=auth_headers,
    )
    assert too_small.status_code == 422

    too_big = await client.get(
        "/me/history",
        params={"tg_user_id": 48, "limit": 101},
        headers=auth_headers,
    )
    assert too_big.status_code == 422


async def test_get_my_history_validates_offset_non_negative(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """``offset`` must be >= 0. A negative offset would break the
    repository's slice, so reject at the route boundary."""
    src_id = "tg:" + "h" * 32
    _seed_onboarded(fake_session, tg_user_id=49, source_id=src_id)

    response = await client.get(
        "/me/history",
        params={"tg_user_id": 49, "offset": -1},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_get_my_history_unknown_user_404s(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """An un-onboarded user gets a 404."""
    response = await client.get(
        "/me/history",
        params={"tg_user_id": 99_998},
        headers=auth_headers,
    )
    assert response.status_code == 404
