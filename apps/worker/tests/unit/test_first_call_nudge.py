"""Unit tests for ``run_first_call_nudges`` — the Q-031 activation cron.

Collaborators (session factory, ``EngagementRepository``,
``SourceIdentityRepository``, ``TelegramNotifier``) are monkeypatched on
their defining modules so the test never touches Postgres / Telegram.
We pin the orchestration:

1. Every eligible source gets the nudge text and a recorded row.
2. Nothing eligible -> 0, no DMs.
3. A source with no identity row is skipped (no DM, no record).
4. A record failure isolates that source (no DM) but doesn't stop the
   rest — and proves record-then-send (a failed record means no send).
5. The sync ``send_first_call_nudges`` entry bridges to ``run_sweep``.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _patch_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_session_factory(_dsn: str) -> Any:
        return _FakeSession

    monkeypatch.setattr(
        "merlin_infrastructure.db.session.get_session_factory",
        fake_get_session_factory,
    )


class _FakeNotifier:
    sent: ClassVar[list[tuple[int, str]]] = []

    def __init__(self, *, bot_token: str) -> None:
        pass

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        _FakeNotifier.sent.append((tg_user_id, text))


def _make_repo(targets: list[str], *, fail_for: set[str] | None = None) -> tuple[type, list[str]]:
    """Build a fake EngagementRepository class + the list it records into."""
    recorded: list[str] = []
    fail = fail_for or set()

    class FakeEngagementRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def list_first_call_nudge_targets(self, **_kwargs: Any) -> list[str]:
            return list(targets)

        async def record_nudge(self, *, source_id: str, nudge_kind: str = "first_call") -> None:
            if source_id in fail:
                raise RuntimeError("simulated record failure")
            recorded.append(source_id)

    return FakeEngagementRepo, recorded


def _make_identity_repo(tg_map: dict[str, int]) -> type:
    class FakeIdentityRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_tg_user_ids_by_source_ids(self, source_ids: list[str]) -> dict[str, int]:
            return {sid: tg_map[sid] for sid in source_ids if sid in tg_map}

    return FakeIdentityRepo


@pytest.fixture(autouse=True)
def _reset() -> None:
    _FakeNotifier.sent = []


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    targets: list[str],
    tg_map: dict[str, int],
    fail_for: set[str] | None = None,
) -> list[str]:
    _patch_session_factory(monkeypatch)
    repo_cls, recorded = _make_repo(targets, fail_for=fail_for)
    monkeypatch.setattr("merlin_infrastructure.db.repositories.EngagementRepository", repo_cls)
    monkeypatch.setattr(
        "merlin_infrastructure.db.repositories.SourceIdentityRepository",
        _make_identity_repo(tg_map),
    )
    monkeypatch.setattr(
        "merlin_infrastructure.notify.telegram.notifier.TelegramNotifier", _FakeNotifier
    )
    return recorded


async def test_nudges_every_eligible_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from merlin_infrastructure.notify.telegram.templates import first_call_nudge

    recorded = _wire(
        monkeypatch,
        targets=["tg:aaa", "tg:bbb"],
        tg_map={"tg:aaa": 111, "tg:bbb": 222},
    )

    from merlin_worker.jobs.first_call_nudge import run_first_call_nudges

    sent = await run_first_call_nudges()

    assert sent == 2
    assert recorded == ["tg:aaa", "tg:bbb"]
    assert _FakeNotifier.sent == [(111, first_call_nudge()), (222, first_call_nudge())]


async def test_noop_when_nothing_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, targets=[], tg_map={})

    from merlin_worker.jobs.first_call_nudge import run_first_call_nudges

    assert await run_first_call_nudges() == 0
    assert _FakeNotifier.sent == []


async def test_skips_source_without_identity_row(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _wire(
        monkeypatch,
        targets=["tg:aaa", "tg:ghost"],
        tg_map={"tg:aaa": 111},  # tg:ghost has no identity row
    )

    from merlin_worker.jobs.first_call_nudge import run_first_call_nudges

    sent = await run_first_call_nudges()

    assert sent == 1
    assert recorded == ["tg:aaa"]
    assert _FakeNotifier.sent == [(111, _FakeNotifier.sent[0][1])]


async def test_record_failure_isolates_and_skips_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _wire(
        monkeypatch,
        targets=["tg:aaa", "tg:bbb"],
        tg_map={"tg:aaa": 111, "tg:bbb": 222},
        fail_for={"tg:aaa"},  # recording aaa blows up
    )

    from merlin_worker.jobs.first_call_nudge import run_first_call_nudges

    sent = await run_first_call_nudges()

    # aaa's record failed -> it is NOT sent (record-then-send); bbb still
    # goes out. Proves the ordering: a failed record gates the DM.
    assert sent == 1
    assert recorded == ["tg:bbb"]
    assert [tg for tg, _t in _FakeNotifier.sent] == [222]


def test_sync_entry_bridges_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run() -> int:
        return 7

    monkeypatch.setattr("merlin_worker.jobs.first_call_nudge.run_first_call_nudges", fake_run)

    from merlin_worker.jobs.first_call_nudge import send_first_call_nudges

    assert send_first_call_nudges() == 7
