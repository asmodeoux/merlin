"""Unit tests for ``GET /board``.

The route is a thin pass-through to
``SourceReputationRepository.leaderboard``, which reads the latest
snapshot per source from ``source_reputation_history``. Cold-start
sources are omitted (no snapshot rows). The shape + pass-through are
pinned here so when the worker starts writing snapshots, the bot
renders the populated leaderboard with no code change.

We also verify the ``window`` query param is enforced by FastAPI's
validation (so the bot can't accidentally pass ``window=lifetime``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from merlin_api.app import create_app
from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings
from merlin_core.application.ports import LeaderboardEntry

TEST_TOKEN = "test-internal-token"


@dataclass
class _FakeReputationRepo:
    leaderboard_rows: list[LeaderboardEntry] = field(default_factory=list)
    window_calls: list[str] = field(default_factory=list)
    horizon_calls: list[str] = field(default_factory=list)

    async def leaderboard(
        self,
        window: str,
        horizon: str = "all",
    ) -> list[LeaderboardEntry]:
        self.window_calls.append(window)
        self.horizon_calls.append(horizon)
        return list(self.leaderboard_rows)


@dataclass
class _FakeClaimRepo:
    """Just the surface ``/board`` touches: ``count_open_global``."""

    open_calls_total: int = 0
    open_callers_total: int = 0
    count_open_global_calls: int = 0

    async def count_open_global(self) -> tuple[int, int]:
        self.count_open_global_calls += 1
        return self.open_calls_total, self.open_callers_total


@dataclass
class _FakeSession:
    reputation: _FakeReputationRepo = field(default_factory=_FakeReputationRepo)
    claims: _FakeClaimRepo = field(default_factory=_FakeClaimRepo)

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def get(self, *_args: Any, **_kw: Any) -> None:
        return None


@pytest.fixture
def fake_session() -> _FakeSession:
    return _FakeSession()


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
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    from merlin_infrastructure.db import repositories as _repos_mod

    monkeypatch.setattr(
        _repos_mod,
        "SourceReputationRepository",
        lambda _s: fake_session.reputation,
    )
    monkeypatch.setattr(
        _repos_mod,
        "ClaimRepository",
        lambda _s: fake_session.claims,
    )

    async def _fake_get_session() -> AsyncIterator[_FakeSession]:
        yield fake_session

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = _fake_get_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


async def test_get_board_returns_empty_list_cold_start(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """No reputation snapshots yet → empty list.

    The bot renders the 'leaderboard unlocks after the first
    resolutions' empty-state. Route must NOT 503 here — empty is the
    honest signal.
    """
    response = await client.get("/board", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["window"] == "all_time"
    assert fake_session.reputation.window_calls == ["all_time"]


async def test_get_board_passes_window_param_to_repo(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """``window`` round-trips: the bot's choice arrives at the repo."""
    response = await client.get(
        "/board",
        params={"window": "30d"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "30d"
    assert fake_session.reputation.window_calls == ["30d"]


async def test_get_board_rejects_invalid_window(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """FastAPI's regex validation must catch a malformed ``window``
    before the repo runs — we don't want stray strings reaching the
    SQL layer."""
    response = await client.get(
        "/board",
        params={"window": "lifetime"},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_get_board_defaults_horizon_to_all(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Pre-filter clients still call ``/board`` → horizon defaults to ``"all"``.

    Pins that the new query param is genuinely optional — a stale
    bot release that knows nothing about horizons must keep working.
    """
    response = await client.get("/board", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon"] == "all"
    assert fake_session.reputation.horizon_calls == ["all"]


@pytest.mark.parametrize("horizon", ["all", "day", "swing", "position"])
async def test_get_board_passes_horizon_param_to_repo_and_echoes_it(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    horizon: str,
) -> None:
    """``horizon`` round-trips end-to-end.

    The repo receives the bot's choice (so the SQL slice can run);
    the response echoes it back (so the bot can render the active
    filter in the header).
    """
    response = await client.get(
        "/board",
        params={"horizon": horizon},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon"] == horizon
    assert fake_session.reputation.horizon_calls == [horizon]


@pytest.mark.parametrize(
    "bad_horizon",
    [
        # Legacy 2-bucket vocabulary — explicit regression guard so an
        # old client tapping a stale ``board:filter:short`` callback
        # gets cleanly rejected at the route layer rather than reaching
        # the repository's ``ValueError``.
        "short",
        "long",
        # Generic invalid values.
        "medium",
        "ALL",
        "24h",
        "intraday",
        "long_term",
        "",
    ],
)
async def test_get_board_rejects_invalid_horizon(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    bad_horizon: str,
) -> None:
    """FastAPI's regex must catch a malformed ``horizon`` before the repo.

    Equivalently: a typo / hostile client can never smuggle an
    unknown bucket through to the SQL layer.
    """
    response = await client.get(
        "/board",
        params={"horizon": bad_horizon},
        headers=auth_headers,
    )
    assert response.status_code == 422
    # The repo was never reached — confirms the regex gates the param
    # before the dependency-injected fake gets a turn.
    assert fake_session.reputation.horizon_calls == []


async def test_get_board_renders_populated_entries(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """When the repo returns rows the route projects them cleanly.

    Seeds two ``LeaderboardEntry``s and asserts the wire payload shape
    — pins the contract so a future schema/serialization tweak fails
    here before reaching the bot.
    """
    fake_session.reputation.leaderboard_rows = [
        LeaderboardEntry(
            rank=1,
            display_name="Alice",
            calls_resolved=10,
            calls_correct=8,
            accuracy_pct=80.0,
            weighted_pnl_pct=4.2,
            last_call_at=datetime.now(tz=UTC),
        ),
        LeaderboardEntry(
            rank=2,
            display_name="Bob",
            calls_resolved=7,
            calls_correct=4,
            accuracy_pct=57.1,
            weighted_pnl_pct=-1.5,
            last_call_at=None,
        ),
    ]
    response = await client.get("/board", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 2
    assert body["entries"][0]["display_name"] == "Alice"
    assert body["entries"][0]["accuracy_pct"] == 80.0
    assert body["entries"][0]["weighted_pnl_pct"] == 4.2
    assert body["entries"][1]["weighted_pnl_pct"] == -1.5
    assert body["entries"][1]["last_call_at"] is None


async def test_get_board_returns_open_activity_counts(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """``open_calls_total`` / ``open_callers_total`` come straight off
    ``ClaimRepository.count_open_global`` and arrive on the wire.

    Pinned because the bot footer ("X open calls by Y users are in
    progress.") reads exactly these two fields — a regression that
    drops either one would silently revert the leaderboard heartbeat
    to the old "Ranked by ..." sentence on its own.
    """
    fake_session.claims.open_calls_total = 22
    fake_session.claims.open_callers_total = 4
    response = await client.get("/board", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["open_calls_total"] == 22
    assert body["open_callers_total"] == 4
    # The route MUST run the count exactly once per /board hit so a
    # busy leaderboard doesn't fan out into N extra queries.
    assert fake_session.claims.count_open_global_calls == 1


async def test_get_board_open_activity_defaults_to_zero_on_cold_start(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
) -> None:
    """Cold-start DB (no open claims) → both counts are 0.

    The bot omits the activity sentence entirely when ``open_calls_total``
    is 0 (a "0 open calls by 0 users" footer reads as a downtime
    signal); pinning the zero echo here keeps the wire contract honest
    so the bot's omission branch fires deterministically.
    """
    # _FakeClaimRepo defaults both totals to 0; no setup needed.
    response = await client.get("/board", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["open_calls_total"] == 0
    assert body["open_callers_total"] == 0


@pytest.mark.parametrize("horizon", ["all", "day", "swing", "position"])
async def test_get_board_open_activity_not_horizon_filtered(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    fake_session: _FakeSession,
    horizon: str,
) -> None:
    """Open-activity counts are platform-wide regardless of ``horizon``.

    The bot surfaces them as a heartbeat that's invariant across
    filter taps — a Day Traders board still shows the overall
    platform activity. ``count_open_global`` takes no horizon arg
    by design; this test pins that the route doesn't accidentally
    start filtering them downstream.
    """
    fake_session.claims.open_calls_total = 17
    fake_session.claims.open_callers_total = 3
    response = await client.get(
        "/board",
        params={"horizon": horizon},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["open_calls_total"] == 17
    assert body["open_callers_total"] == 3
