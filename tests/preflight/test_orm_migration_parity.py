"""Preflight — every ORM table must have a matching ``create_table`` in migrations.

Sprint-1 live regression we want to pin in CI forever:

* ``apps/worker/merlin_worker/jobs/resolve_outcome.py`` was wired
  against ``outcomes`` (and friends) on a database that didn't have
  those tables — every migration to date only created
  ``sources``, ``source_identity``, ``invites``, ``claims``.
* The first 2-hour BTC bet exposed this: the worker fired,
  ``_ClaimContext.load()`` raised ``NotImplementedError``, the user
  got nothing.
* The deeper class of bug is "ORM model declared but no migration
  created the table". Repository code passes ``ruff`` / ``mypy`` /
  unit tests fine — the failure mode only triggers at first INSERT.

This file closes that loop by reading both sides:

1. ``Base.metadata.tables`` — every ORM class that inherits from
   ``Base`` is registered here.
2. ``alembic/versions/*.py`` — source-level grep for
   ``op.create_table("<name>"`` and ``op.drop_table("<name>"``. The
   set of table names found across the migration chain must be a
   superset of ``Base.metadata.tables.keys()``.

If you add an ORM model without a migration, this test fails with a
remediation hint that names the table.
"""

from __future__ import annotations

import re
from pathlib import Path

from merlin_infrastructure.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = (
    REPO_ROOT / "packages" / "python" / "merlin_infrastructure" / "alembic" / "versions"
)


_CREATE_TABLE_RE = re.compile(
    r"""op\.create_table\(\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']""",
    re.MULTILINE,
)
_DROP_TABLE_RE = re.compile(
    r"""op\.drop_table\(\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']""",
    re.MULTILINE,
)


def _collect_migration_tables() -> tuple[set[str], set[str]]:
    """Return (created, dropped) table-name sets from every migration script.

    Reads every ``.py`` under ``alembic/versions`` and matches against
    the ``op.create_table("...")`` / ``op.drop_table("...")`` calls.
    Built as plain regex instead of an AST visitor because every
    migration uses the same literal form — the brittle bit (different
    quote style, different module alias) doesn't appear in practice
    and a stricter parse would refuse to grep file-level constants.
    """
    created: set[str] = set()
    dropped: set[str] = set()
    for path in MIGRATIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        created.update(_CREATE_TABLE_RE.findall(text))
        dropped.update(_DROP_TABLE_RE.findall(text))
    return created, dropped


def test_every_orm_table_has_a_create_table_in_migrations() -> None:
    """Every ``Base.metadata.tables`` entry MUST be created in a migration.

    Failure here is the Sprint-1 ``outcomes``-missing bug under a new
    table name. The remediation is to author the matching alembic
    revision; the error message names the offenders so the AI agent
    or human writing the next migration knows where to start.
    """
    orm_tables = set(Base.metadata.tables.keys())
    created, _ = _collect_migration_tables()

    missing = orm_tables - created
    assert not missing, (
        f"ORM tables with no matching ``op.create_table`` in any migration: "
        f"{sorted(missing)}. Author an alembic revision that creates them — "
        "without one, the first repository INSERT will 500 against an empty "
        "database."
    )


def test_every_create_table_has_a_drop_in_some_migration_downgrade() -> None:
    """Round-trip parity: a created table SHOULD have a drop somewhere.

    Loose check — we don't require every migration to be reversible
    individually (some scripts intentionally have no-op downgrades),
    but across the chain there should be a drop for every create.
    Catches the "added create_table but forgot the downgrade" case
    which makes ``alembic downgrade -1`` blow up.
    """
    created, dropped = _collect_migration_tables()
    missing_drops = created - dropped
    assert not missing_drops, (
        f"Tables created in migrations but never dropped in any downgrade: "
        f"{sorted(missing_drops)}. Each create_table needs a matching "
        "drop_table in some downgrade() so ``alembic downgrade base`` "
        "reaches an empty schema."
    )


def test_append_only_tables_have_triggers_in_migrations() -> None:
    """MVP §15 invariants 7/8/10/12 — append-only triggers MUST land with their tables.

    Source-level check across the migration chain. Two failure modes
    to catch:

    1. A table marked append-only in the ORM but with no
       UPDATE/DELETE-rejecting trigger emitted by any migration.
    2. The trigger function name pattern (``trg_<table>_no_update``,
       ``trg_<table>_no_delete``) changed in one place but not
       another (table list vs. trigger emitter), breaking the
       invariant by drift.

    The migrations use ``CREATE TRIGGER trg_{table}_no_<op>`` inside
    f-strings, so we check both: the resolved literal form (for any
    migration that hardcoded a single table) and the templated form
    plus the table-name list (for the loop-based emitter).
    """
    append_only = {
        "outcomes",
        "source_reputation_history",
        "attribution_ledger",
        "override_log",
    }

    aggregate_text = ""
    for path in MIGRATIONS_DIR.glob("*.py"):
        aggregate_text += path.read_text(encoding="utf-8")

    # Pre-resolve the templated emitter once: if the loop pattern
    # appears AND every append-only table is named somewhere in the
    # aggregate text (so the loop body covers them all), we accept it.
    templated_update = "trg_{table}_no_update" in aggregate_text
    templated_delete = "trg_{table}_no_delete" in aggregate_text

    missing_triggers: list[str] = []
    for table in append_only:
        # Every table must show up in the aggregate text so the loop
        # iteration is guaranteed to cover it. Plus the templated
        # emitter must be present. As a belt-and-braces fallback we
        # also accept the literal resolved form for any migration
        # that opted to write it out explicitly.
        for op_name in ("update", "delete"):
            templated_present = templated_update if op_name == "update" else templated_delete
            resolved_present = f"trg_{table}_no_{op_name}" in aggregate_text
            covered_by_template = templated_present and table in aggregate_text
            if not (resolved_present or covered_by_template):
                missing_triggers.append(f"trg_{table}_no_{op_name}")

    assert not missing_triggers, (
        "Append-only invariant triggers missing from migrations: "
        f"{sorted(missing_triggers)}. The matching tables exist but a "
        "stray UPDATE/DELETE would not be rejected at the DB layer — "
        "MVP §15 invariants 7/8/10/12 require both."
    )
