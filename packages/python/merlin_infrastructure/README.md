# merlin_infrastructure

Heavy I/O adapters for the Merlin platform. Implements the Protocols
defined in `merlin_core.application.ports`:

- `db/` — SQLAlchemy 2.x async models + session + repositories.
- `market_data/` — CoinGecko Demo adapter (httpx-backed). Single
  market-data source per Q-026.
- `jobs/` — RQ helpers (job-registration helpers, NOT job definitions —
  those live in `merlin_worker.jobs/`).
- `observability/` — structlog + Sentry configuration.
- `alembic/` — database migrations (co-located with the package that
  owns the models — see ADR (l) in `docs/ARCHITECTURE.md`).

## Forbidden imports

Enforced by `[tool.importlinter]` in the root `pyproject.toml`:

- No `fastapi` — infrastructure is shared by api and worker; both must
  use the same modules.
- No `telegram` — wrong layer.
- No `merlin_api` / `merlin_bot` / `merlin_worker` — the dependency arrow
  points the other way.

## Postgres extensions

The dev DB ships with one Postgres extension:

- **`vector`** (pgvector) — used for the `vector(384)` similarity column
  on `claims` (embedding from
  `sentence-transformers/all-MiniLM-L6-v2`, 384-dim per).

Q-029 drops `pg_cron` from the v0
stack. Resolution scheduling is `RQ Queue.enqueue_at(t_resolve, …)`
and reputation recompute is reactive (enqueued by the resolver the
moment an outcome lands). The custom image at
`deploy/postgres-dev/Dockerfile` is therefore plain
`pgvector/pgvector:pg16` with the `vector` extension on top — no
`pg_cron` apt install, no `shared_preload_libraries` tweak (Q-029).

## Append-only invariants

Several tables are append-only (ARCHITECTURE §16 invariants 7/8/10/12):

- `claims` (except the `edits` JSONB column and `soft_deleted_at`)
- `outcomes`
- `source_reputation_history`
- `attribution_ledger`
- `override_log`

Two-layer enforcement:

1. **Postgres triggers** on each table raise on disallowed UPDATE/DELETE.
2. **Repository pattern** — the repositories in `db/repositories/` for
   these tables expose no `update()` method; only
   `insert_*_event()`-flavored writes.

The CI test at `tests/integration/test_append_only_invariants.py` IS the
contract — any future migration that removes a trigger fails CI
immediately.

## Source-ID opacity

The PII table `source_identity` holds the raw `tg_user_id`. Every other
table references the source by `source_id`
(`'tg:' + sha256(tg_user_id)[:32]`, see
`merlin_core.domain.source_id.make_source_id`). Repositories that need
to look up a source by Telegram ID accept a `source_id` — they don't
take a raw ID and call `make_source_id` themselves. See ARCHITECTURE §16
invariant 9.

## Layout

Conventional two-layer flat layout. `alembic/` lives at the package
root (not inside the importable package) because migrations are
repository artifacts, not library code:

```
packages/python/merlin_infrastructure/
├── pyproject.toml
├── README.md
├── alembic/                            # migrations — outside the importable package
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
├── tests/
└── merlin_infrastructure/              # the importable package
    ├── __init__.py
    ├── py.typed
    ├── db/{models.py, session.py, repositories/}
    ├── market_data/
    ├── jobs/
    └── observability/
```

`make db-migrate` runs `cd packages/python/merlin_infrastructure/alembic && uv run alembic upgrade head`.

See ADR (q) in `docs/ARCHITECTURE.md` for why we use this layout
instead of the single-layer prefix-add via Hatchling `sources`.
