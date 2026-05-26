# Merlin Architecture

> **BLUF.** Merlin is a polyglot monorepo. Three Python services
> (`api`, `bot`, `worker`) plus three shared Python packages (`core`,
> `infrastructure`, `client`) live in `apps/` and `packages/python/`.
> The bot talks to the API over HTTP; the worker writes to the DB
> directly. A future React frontend plugs into the same `/v1/*` surface
> via OpenAPI-generated TypeScript types. Hexagonal layering is enforced
> by `import-linter`. Dev loop is docker-compose with hot reload and
> debugpy on every service.

---

## 1. Runtime topology

```
┌─────────────────────┐                ┌──────────────────────┐
│ Telegram Bot API    │ <───polling──> │  merlin-bot          │
└─────────────────────┘                │  (PTB v21)           │
                                       │                      │
                                       │  HTTP /v1/* (HMAC)   │
                                       │                      ▼
                                       │           ┌────────────────────┐
                                       │           │  merlin-api        │
                                       │           │  (FastAPI)         │
                                       │           └─────────┬──────────┘
                                       │                     │
                                       │             ┌───────┴───────┐
                                       │             │               │
                                       │             ▼               ▼
                                       │       ┌──────────┐    ┌──────────┐
                                       │       │ Postgres │    │  Redis   │
                                       │       │ pgvector │    │  RQ      │
                                       │       │ pg_cron  │    │          │
                                       │       └────▲─────┘    └────▲─────┘
                                       │            │               │
                                       │            │               │
                                       │            │               │
                                       │       ┌────┴──────────────-┴────┐
                                       │       │  merlin-worker          │
                                       │       │  (RQ + CoinGecko Demo)  │
                                       └─────> │                         │
                                               └─────────────────────────┘

(future) merlin-web (React) ──▶  same /v1/* surface, JWT auth (Phase 2)
(future) merlin-pro (Dashboard) ──▶  same /v1/* surface
```

## 2. Layer model

```
┌───────────────────────────────────────────────────────────────┐
│  merlin_api       merlin_bot       merlin_worker              │  ← App layer
│  (FastAPI)        (PTB v21)        (RQ)                       │
├───────────────┬───────────────────────┬───────────────────────┤
│               │                       │                       │
│         merlin_client                 │                       │
│         (httpx SDK)                   │                       │
│                                       │                       │
├───────────────────────────────────────┼───────────────────────┤
│  merlin_infrastructure                                        │  ← Adapters
│  (SQLAlchemy, asyncpg, alembic, pgvector,                     │
│   httpx market data, structlog, sentry-sdk)                   │
├───────────────────────────────────────────────────────────────┤
│  merlin_core.application (use cases + ports = Protocols)      │  ← Application
├───────────────────────────────────────────────────────────────┤
│  merlin_core.domain (entities, value objects, regret math)    │  ← Domain
│  merlin_core.contracts (Pydantic wire schemas)                │  (pure)
└───────────────────────────────────────────────────────────────┘
```

Layer boundaries are enforced by `[tool.importlinter]` in the root
`pyproject.toml`:

- `merlin_core.domain` — stdlib only
- `merlin_core.application` — `domain` + own ports
- `merlin_core.contracts` — stdlib + pydantic
- `merlin_infrastructure` — no `fastapi` / `telegram`
- `merlin_client` — `merlin_core.contracts` + httpx
- `merlin_api` — no `telegram`
- `merlin_bot` — only `merlin_client` + `merlin_core.contracts`
- `merlin_worker` — no `fastapi` / `telegram`

CI runs `lint-imports` as a hard gate.

## 3. Repo layout

```
.
├── apps/
│   ├── api/                    # FastAPI server
│   │   ├── pyproject.toml      # merlin-api
│   │   ├── Dockerfile          # base / dev / prod
│   │   ├── fly.toml
│   │   ├── .env.example
│   │   ├── README.md
│   │   ├── merlin_api/         # importable package
│   │   │   ├── __init__.py
│   │   │   ├── py.typed
│   │   │   ├── main.py
│   │   │   ├── app.py
│   │   │   ├── config.py
│   │   │   ├── openapi.py
│   │   │   ├── auth.py
│   │   │   ├── deps.py
│   │   │   ├── errors.py
│   │   │   ├── health.py
│   │   │   └── routes/
│   │   ├── scripts/
│   │   │   └── generate_openapi.py
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── unit/
│   │       ├── integration/
│   │       └── snapshot/
│   ├── bot/                    # Telegram bot (mirrored shape)
│   └── worker/                 # RQ worker (mirrored shape)
├── packages/
│   └── python/                 # room for packages/ts/ later (React frontend)
│       ├── merlin_core/        # two-layer flat — see ADR (q)
│       │   ├── pyproject.toml
│       │   ├── README.md
│       │   ├── tests/                      # outside the importable package
│       │   └── merlin_core/                # importable package
│       │       ├── __init__.py
│       │       ├── py.typed
│       │       ├── domain/
│       │       ├── application/{ports,use_cases}/
│       │       └── contracts/
│       ├── merlin_infrastructure/   # same shape; alembic/ at package root
│       │   ├── alembic/                    # migrations (outside the package)
│       │   └── merlin_infrastructure/      # importable package
│       └── merlin_client/           # same shape
├── deploy/
│   ├── postgres-dev/           # custom pgvector + pg_cron image
│   ├── docker-compose.dev.yml
│   ├── docker-compose.test.yml
│   └── README.md
├── scripts/
│   ├── env_check.py
│   └── bootstrap_github_settings.sh
├── docs/
│   ├── ARCHITECTURE.md         # ← this file
│   ├── MVP_SPEC.md
│   ├── RUNBOOK.md
│   ├── REPO_SETTINGS.md
│   └── openapi.json            # snapshot
├── .github/
│   ├── workflows/{ci,security,docker-test}.yml
│   ├── CODEOWNERS
│   ├── dependabot.yml
│   └── pull_request_template.md
├── .vscode/{launch,tasks,settings,extensions}.json
├── AGENTS.md
├── SECURITY.md
├── README.md
├── pyproject.toml              # workspace coordinator (no [project] table)
├── uv.lock                     # committed
├── .python-version             # 3.12
├── Makefile
├── .gitignore
├── .editorconfig
└── .pre-commit-config.yaml
```

## 4. Polyglot-monorepo conventions

- **Apps go in `apps/<slug>/`.** Each app is independently deployable
  and ships its own Dockerfile + fly.toml + .env.example.
- **Shared code goes in `packages/<lang>/<name>/`.** Today only
  `packages/python/`; when the React frontend lands, `packages/ts/` joins.
- **Tests live with the code they test.** Inside the app or package.
  `tests/unit`, `tests/integration`, `tests/snapshot` are the conventional
  subfolders.
- **Per-app `.env.example`** — no global one. `scripts/env_check.py`
  walks every app and asserts parity with its `Settings` class.
- **One `Makefile` at the root** drives every command. CI, VS Code
  tasks, and the dev loop all funnel through it.

## 5. Why 3 packages, not 5

Splitting `domain`, `application`, and `contracts` into separate PyPI
packages was discussed and rejected:

- One `pyproject.toml` + one wheel for the pure-Python core is simpler
  to install and version. `merlin_core` ships all three submodules
  under one importable name.
- `import-linter` enforces the boundaries equally well as separate
  packages would.
- Contributors think "core" as one concept.

The three packages we ship — `core`, `infrastructure`, `client` —
correspond to the three actual deployment boundaries:

- **`core`** — pure Python, no I/O. Imported by every other package.
- **`infrastructure`** — heavy I/O. Imported only by `api` and `worker`
  (the things that talk to a DB).
- **`client`** — HTTP SDK. Imported only by `bot` (and future Python
  integrations).

## 6. Multi-frontend strategy

Today the bot is the only frontend. Tomorrow there will be three:

1. **Telegram bot** (today). HTTP client of the API via `merlin_client`.
2. **React frontend** (Phase 2). HTTP client via TypeScript types
   generated from `docs/openapi.json` with `openapi-typescript`. Will
   live at `packages/ts/openapi-client/` and `apps/web/`. Auth via JWT
   (the second security scheme we add to OpenAPI; HMAC stays for
   service-to-service).
3. **Pro dashboard** (Phase 3). Same `/v1/*` surface, same OpenAPI
   client, separate React app. May share components from `apps/web` via
   a `packages/ts/ui/` shared lib.

The constant: **the API is the contract**. Every client builds on top
of `docs/openapi.json`. We never branch the schema for a frontend.

## 7. /alpha sequence diagram (Sprint 1 reference)

```
User                Bot              merlin_client    API              DB
 │                   │                     │            │                │
 │── /alpha ────────▶│                     │            │                │
 │                   │  enter conv state   │            │                │
 │◀── asset picker ──│                     │            │                │
 │── select BTC ────▶│                     │            │                │
 │                   │ ...                 │            │                │
 │── thesis text ───▶│                     │            │                │
 │                   │── POST /v1/claims ─▶│            │                │
 │                   │                     │── auth ──▶│                 │
 │                   │                     │            │── INSERT ────▶│
 │                   │                     │            │◀── id ─────────│
 │                   │                     │◀── 201 ────│                │
 │                   │◀── ClaimResponse ───│            │                │
 │◀── receipt + ID ──│                     │            │                │
 │                   │                     │            │  (later)       │
 │                   │                     │            │   worker       │
 │                   │                     │            │   resolves     │
 │                   │                     │            │   outcome ─────│
```

## 8. OpenAPI + client-SDK strategy

- `merlin_api.openapi.build_openapi(app)` is the single source of
  truth.
- `apps/api/scripts/generate_openapi.py` writes `docs/openapi.json` —
  committed, deterministic (sorted keys, 2-space indent).
- Snapshot test (`apps/api/tests/snapshot/test_openapi.py`) catches
  drift in CI; `make openapi-check` runs the same diff locally.
- Future React frontend regenerates a TypeScript client from
  `docs/openapi.json` via `openapi-typescript` and ships it as
  `packages/ts/openapi-client/`. Same `/v1/*` surface, same types, no
  hand-written client.

## 9. PTB v21 bot structure

Patterns we follow (`merlin_bot/`):

- **`Application`** as the central object. No `Updater`.
- **`AIORateLimiter`** wired in via `ApplicationBuilder.rate_limiter()`
  — TG's 30 msg/s/bot ceiling enforced automatically.
- **`Defaults(parse_mode="MarkdownV2", tzinfo=UTC, block=False)`** —
  consistent rendering across handlers.
- **`BasePersistence`** — `RedisPersistence` in prod, `DictPersistence`
  in dev. **Never PicklePersistence in prod** — doesn't survive
  horizontal scaling.
- **`ConversationHandler`** with explicit `IntEnum` states for `/alpha`.
- **`CallbackQueryHandler`** with PTB's typed `CallbackData` — no string
  parsing.
- **`set_my_commands`** on startup via `post_init`, with admin-scoped
  variant via `BotCommandScopeChat(chat_id=admin_id)`.
- **`add_error_handler`** → Sentry with PII stripped (raw Telegram
  handles never leave the process; MVP §15 invariant 9).
- **`run_webhook(secret_token=...)`** in prod; `run_polling()` in dev.
- **`bot_data["merlin_client"]`** holds the `MerlinClient` — no globals,
  single httpx connection pool.

## 10. Security baseline (v0)

Full details in `SECURITY.md` and `docs/REPO_SETTINGS.md`. Summary:

- **Lockfile** — `uv.lock` committed; `uv lock --check` in CI.
- **Cooldowns** — Dependabot 7-day default / 30d major / 7d minor / 3d
  patch.
- **Scanners** — gitleaks (pre-commit + CI), pip-audit (CI + daily
  cron), Dependency Review (PR diff, blocks moderate+).
- **GitHub Secret Scanning + Push Protection** — both enabled at the
  repo level.
- **Workflow permissions** — top-of-file `contents: read`; widened
  per-job only when strictly required.
- **Service tokens** — `fly secrets set`; never committed.
- **AI disclosure** — required in the PR template; CI applies the same
  gates to AI-generated code.
- **Append-only DB tables** (MVP §15 7/8/10/12) — Postgres triggers +
  repository pattern + `tests/integration/test_append_only_invariants.py`.
- **Source-ID opacity** (MVP §15 9) — raw Telegram IDs live only in
  `source_identity` table; everything else uses `source_id`.

## 11. Docker-first build / deploy

- **One Dockerfile per app**, three targets — `base` / `dev` / `prod`.
- **Dev compose** brings up all five services (postgres + redis +
  api/bot/worker) with hot reload + debugpy on dedicated ports.
- **Test compose** builds prod-target images and runs the integration
  suite via a runner sidecar — the same images Fly serves.
- **Fly** — `fly deploy --config apps/<svc>/fly.toml`; `[build.args]
  TARGET=prod` pinned in fly.toml so Fly never accidentally ships a
  dev image.

## 12. Env-var configuration model

- Each app has **its own** `Settings(BaseSettings)` at
  `merlin_<svc>/config.py`.
- Each app has **its own** `.env.example` at `apps/<svc>/.env.example`.
- `scripts/env_check.py` asserts every Settings field has a matching
  `.env.example` key and vice-versa. CI gate.
- No root `.env.example`. Different apps get different env vars; one
  pot would dilute the signal.

## 13. LLM conventions (pointer)

`AGENTS.md` is the authoritative pointer for AI agents working in this
repo. Key conventions:

- **Source of truth** — `MVP_SPEC.md`, this doc, `RUNBOOK.md`,
  `REPO_SETTINGS.md`.
- **5-step ritual** for adding a feature that touches the bot: route →
  schema → client method → openapi snapshot → handler.
- **Layer rules** — `import-linter` rejects violations in CI.
- **Append-only invariants** — never UPDATE on the protected tables.
- **PII** — never log raw Telegram handles.
- **OpenAPI** — every route change requires regenerating
  `docs/openapi.json`.
- **Env vars** — Settings + .env.example move together.

## 14. ADRs

| ID  | Decision                                                                                      | Date     |
| --- | --------------------------------------------------------------------------------------------- | -------- |
| a   | Polyglot monorepo (not multi-repo). One `git pull`, one PR for cross-cutting changes.         | 2026-05  |
| b   | Three Python apps, three shared packages. No `src/` layer in apps; flat single-layer pkgs.     | 2026-05  |
| c   | `import-linter` enforces hexagonal layers. Hard CI gate.                                       | 2026-05  |
| d   | `merlin_core` is one package (not three). Internal submodules; same boundaries via importer.   | 2026-05  |
| e   | RQ for background jobs (not arq / dramatiq). MVP §8.1 — simpler glue for v0.                   | 2026-05  |
| f   | HMAC bearer auth for service-to-service in v0. JWT added as a second scheme in Phase 2.        | 2026-05  |
| g   | OpenAPI snapshot committed at `docs/openapi.json`. Snapshot test + `make openapi-check`.        | 2026-05  |
| h   | Per-app .env.example; no root one. `scripts/env_check.py` enforces parity with Settings.       | 2026-05  |
| i   | Three Dockerfile targets per app: base / dev / prod. Fly pins TARGET=prod via `[build.args]`.  | 2026-05  |
| j   | Tests live with the app/package they test. No top-level `tests/` directory.                    | 2026-05  |
| k   | pg_cron required for daily reputation recompute (MVP §8.1). Custom postgres-dev image adds it. | 2026-05  |
| l   | Alembic migrations co-located with `merlin_infrastructure`. The package that owns the models. | 2026-05  |
| m   | uv (not Poetry / pip-tools). Speed + native workspace support.                                 | 2026-05  |
| n   | Dependabot cooldown 7-day default. Catches first-week regressions before our auto-PRs land.    | 2026-05  |
| o   | gitleaks + pip-audit + Dependency Review as the v0 scanner stack. osv-scanner / trufflehog Ph2| 2026-05  |
| p   | Renovate noted as Dependabot fallback. Only flip if Dependabot's cooldown story regresses.    | 2026-05  |
| q   | Two-layer flat package layout (`packages/python/merlin_X/merlin_X/`). Single-layer rejected — see long form. | 2026-05  |

ADR (q) — long form:

> **First attempt (rejected).** We tried a single-layer flat layout
> (`packages/python/merlin_core/{domain,application,contracts}/`) via
> Hatchling's `sources` mapping `"" = "merlin_core"`. This builds a
> correct wheel but **breaks editable installs** — `uv sync` (which uses
> editable installs by default) fails with `ValueError: Dev mode
> installations are unsupported when any path rewrite in the sources
> option changes a prefix rather than removes it` (see
> [pfmoore/editables#20](https://github.com/pfmoore/editables/issues/20)).
> Editable installers can _remove_ a prefix (`src/foo` → `foo`) but
> can't _add_ one — there's no real directory on disk for the loader to
> alias to `merlin_core/`.
>
> **Final choice.** Conventional two-layer flat layout:
>
> ```
> packages/python/merlin_core/
> ├── pyproject.toml
> ├── tests/
> └── merlin_core/                # the importable package
>     ├── __init__.py
>     ├── py.typed
>     ├── domain/
>     ├── application/
>     └── contracts/
> ```
>
> Build config is the conventional one-liner:
>
> ```toml
> [tool.hatch.build.targets.wheel]
> packages = ["merlin_core"]
> ```
>
> Trade-off: one extra `merlin_core/` subfolder per package. Benefit:
> editable installs work, no Hatchling tricks, standard Python pattern
> every Python tool/contributor expects.
>
> `merlin_infrastructure/alembic/` lives at the **package root**
> (alongside the inner `merlin_infrastructure/` Python package) — not
> inside the importable package — because migrations are repository
> artifacts, not library code. `make db-migrate` cds into that
> directory directly.

## 15. Future-proofing notes

- **`packages/ts/`** — when the React frontend lands, mirror the
  `packages/python/` shape. `packages/ts/openapi-client/` from
  `openapi-typescript`. `packages/ts/ui/` shared component lib (Phase
  3 Pro dashboard).
- **`apps/web/`** — React + Vite + Tailwind (TBD in Sprint when we
  start it). Same Dockerfile pattern (base/dev/prod), same fly.toml
  shape.
- **JWT scheme** — added to `merlin_api.openapi.SECURITY_SCHEMES`
  alongside `bearerAuth` when the frontend lands. HMAC stays for
  service-to-service.
- **Pro dashboard** — Phase 3. Likely a separate `apps/web-pro/` with a
  different auth gate. Reuses `packages/ts/openapi-client/` and
  potentially `packages/ts/ui/`.
- **Stronger coverage gates** — Sprint 3 ADR ratchets coverage
  thresholds to steady-state (domain & contracts 100% / application &
  api.routes & client 95% / db.repositories & worker 90% / bot 80%).
- **Off-Fly external backup** — Phase 2. v0 relies on Fly Postgres
  daily snapshots only (MVP §12.8).
