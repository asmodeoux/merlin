"""Append-only invariants — MVP §15 invariants 7/8/10/12.

Tables under contract:

- `claims` (except the `edits` JSONB column + `soft_deleted_at`)
- `outcomes`
- `source_reputation_history`
- `attribution_ledger`
- `override_log`

Two-layer enforcement that this test polices:

1. **Repository pattern** — `XxxRepository` exposes no `update()` /
   `delete()` method, only `insert_xxx_event()`-flavored writes.
2. **Postgres triggers** — the initial Alembic migration adds triggers
   that raise on UPDATE/DELETE of these tables. Sprint 1 lands the
   triggers; Sprint 0 lands this test as the contract that catches a
   future migration removing them.

Sprint 1 fills the test body. The scaffold here marks the test
`integration` (it needs a live DB) and ships a `xfail(reason="Sprint 1")`
gate so CI doesn't fail before the implementation lands but also
doesn't silently pass.
"""

from __future__ import annotations

import pytest

APPEND_ONLY_TABLES = (
    "claims",
    "outcomes",
    "source_reputation_history",
    "attribution_ledger",
    "override_log",
)


@pytest.mark.integration
@pytest.mark.append_only
@pytest.mark.xfail(reason="Sprint 1 — wire triggers + repository assertions.", strict=False)
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_update_raises_on_append_only_table(table: str) -> None:
    """An UPDATE on an append-only table must be rejected.

    Sprint 1 implementation:

    1. Connect via the merlin_infrastructure async session.
    2. INSERT one row (using the repository's insert helper).
    3. Attempt a raw UPDATE on the inserted row.
    4. Assert it raises (Postgres trigger → SQLAlchemy IntegrityError /
       ProgrammingError, depending on trigger style).
    5. Repeat for DELETE.

    For `claims`, the test must also assert UPDATE on `edits` JSONB
    + UPDATE on `soft_deleted_at` are *allowed* (those are the two
    excepted columns).
    """
    pytest.fail(f"Sprint 1 — write the UPDATE-rejection test for `{table}`.")


@pytest.mark.fast
def test_append_only_table_list_documented_here_matches_mvp_spec() -> None:
    """Sanity-check the canonical list — any change is a deliberate
    invariant change (MVP §15 invariants 7/8/10/12) and must be reviewed
    accordingly.
    """
    assert set(APPEND_ONLY_TABLES) == {
        "claims",
        "outcomes",
        "source_reputation_history",
        "attribution_ledger",
        "override_log",
    }
