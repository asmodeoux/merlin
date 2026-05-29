# AGENTS.md

Operational guide for AI coding agents (Cursor, Claude, Codex, ...) and
human contributors working in this repo. Read this first.

## 1. Source of truth

Treat these files as canonical. If they disagree with what you "know",
the file wins:

- **[`docs/MVP_SPEC.md`](docs/MVP_SPEC.md)** — product spec; what we
  build and what the invariants are.
- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — how the repo is
  laid out and why. Layer rules, ADRs, the multi-frontend strategy.
- **[`docs/RUNBOOK.md`](docs/RUNBOOK.md)** — operational procedures
  (Sprint 4 fills in the recovery commands pre-launch).
- **[`docs/REPO_SETTINGS.md`](docs/REPO_SETTINGS.md)** — GitHub bootstrap
  checklist.
- **[`SECURITY.md`](SECURITY.md)** — vuln disclosure + v0 security
  baseline.

## 2. Monorepo map

```
apps/        api/  bot/  worker/         ← deployable services (one Dockerfile each)
packages/    python/ merlin_core/         ← shared Python libs
                     merlin_infrastructure/
                     merlin_client/
deploy/      docker-compose dev + test, custom postgres-dev image
scripts/     env_check.py, bootstrap_github_settings.sh
docs/        ARCHITECTURE, MVP_SPEC, RUNBOOK, REPO_SETTINGS, openapi.json
.github/     workflows, CODEOWNERS, dependabot, PR template
.vscode/     launch.json (3 groups), tasks.json (label-prefixed)
```

`docs/ARCHITECTURE.md` §3 has the full tree.

## 3. Layer rules (hard CI gate)

Enforced by `import-linter` (`uv run lint-imports`):

| Module                       | Allowed                                       |
| ---------------------------- | --------------------------------------------- |
| `merlin_core.domain`         | stdlib only                                   |
| `merlin_core.application`    | `merlin_core.domain` + own ports              |
| `merlin_core.contracts`      | stdlib + pydantic                             |
| `merlin_infrastructure`      | anything except `fastapi` / `telegram`        |
| `merlin_client`              | `merlin_core.contracts` + httpx               |
| `merlin_api`                 | anything except `telegram`                    |
| `merlin_bot`                 | `merlin_client` + `merlin_core.contracts` only|
| `merlin_worker`              | anything except `fastapi` / `telegram`        |

If a PR needs to violate a contract, the contract changes in the same
PR with explicit ADR justification in `docs/ARCHITECTURE.md`.

## 4. Bot ↔ API ritual

When the bot needs new data, **always** do these 5 steps in the same PR:

1. **Route** — add the route under `merlin_api.routes.<resource>` with
   `response_model`, 4xx responses, `summary`, and `tags`. Mount it
   under the `/v1` router in `merlin_api/app.py`.
2. **Schema** — define the Pydantic request/response types in
   `merlin_core.contracts.<resource>`.
3. **Client** — add the method to `merlin_client.MerlinClient`.
4. **OpenAPI** — `make openapi`, review the diff, commit
   `docs/openapi.json`.
5. **Handler** — add the bot handler in `merlin_bot.handlers.<feature>`
   that calls the new client method.

Skipping steps creates drift between the spec and the code.

## 5. OpenAPI + coverage policy

- **OpenAPI** — `make openapi` regenerates `docs/openapi.json`. CI runs
  `make openapi-check` and fails on drift.
- **Coverage gates** — per-package, ramped:
  - Sprint 1–2: domain 90% / contracts 95% / application 85% /
    api.routes 85% / client 85% / db.repositories 80% / worker 80% /
    bot 70%.
  - Sprint 3+: 100% / 100% / 95% / 95% / 95% / 90% / 90% / 80%.
- **mypy --strict** scope: `merlin_core`, `merlin_api`, `merlin_client`,
  `merlin_worker`. Soft strict on `merlin_infrastructure` and
  `merlin_bot` (heavy I/O glue; the disruptive flags are off).

## 6. PTB v21 patterns

Bot handlers must follow the patterns in
`docs/ARCHITECTURE.md` §9:

- `Application`, never `Updater`.
- `AIORateLimiter` wired via `ApplicationBuilder`.
- `Defaults(parse_mode="MarkdownV2", tzinfo=UTC, block=False)`.
- `RedisPersistence` in prod, `DictPersistence` in dev. **Never** Pickle.
- `ConversationHandler` + explicit `IntEnum` states for `/alpha`.
- Typed `CallbackData` via `CallbackQueryHandler(pattern=...)`. No
  string parsing.
- `set_my_commands` in `post_init` + admin scope via
  `BotCommandScopeChat`.
- `add_error_handler` → Sentry with PII stripped.
- `bot_data["merlin_client"]` for the API SDK.

## 7. LLM-friendly conventions

- **Module headers**: every package starts with a one-paragraph
  docstring that explains the layer rule.
- **Repository docstrings** flag append-only invariants for AI code
  generators (`merlin_core.application.ports.__init__`).
- **Sprint 1 TODOs** are inline in stubs — the agent can find what's
  unfinished by grep.
- **One Makefile target per workflow**. If you find yourself running a
  long shell pipeline twice, it belongs in the Makefile.

## 8. Security baseline (summary)

Full text in `SECURITY.md` and `docs/REPO_SETTINGS.md`. Highlights:

- `uv.lock` committed; CI runs `uv lock --check`.
- Dependabot 7d cooldown default (30d major, 7d minor, 3d patch).
- gitleaks (pre-commit + CI) + pip-audit (CI + daily) + Dependency
  Review (PR).
- GitHub Secret Scanning + Push Protection on.
- Top-of-file workflow `permissions: contents: read`.
- Branch protection on `main` requires CODEOWNERS + linear history + no
  force-push.
- Service tokens via `fly secrets set`. Never committed.

## 9. Env-var policy

Adding or renaming an env var = **one commit** that updates both
`merlin_<svc>.config.Settings` and `apps/<svc>/.env.example`. `make
env-check` fails CI otherwise.

There is no root `.env.example`. Different apps have different env vars;
one pot dilutes the signal.

## 10. Docker-first dev loop

```bash
make install-frozen     # uv sync --frozen --all-extras
make dev-up             # postgres + redis + api/bot/worker, hot reload + debugpy
make dev-logs           # tail
make docker-test        # prod images + integration suite
```

Hot reload + debugpy ports:

- API     `http://localhost:8000` + debugpy **5678**
- Bot     polling                  + debugpy **5679**
- Worker  RQ verbose               + debugpy **5680**

The `.vscode/launch.json` has "Attach to API/Bot/Worker (Docker debugpy
NNNN)" entries for each. Compound config attaches to all three.

## 11. Repo settings (pointer)

`docs/REPO_SETTINGS.md` is the canonical list. To reproduce on a fresh
clone:

```bash
GH_REPO=org/repo ./scripts/bootstrap_github_settings.sh
```

## 12. Stack pins

- Python **3.12** (`.python-version`).
- uv **0.5.7** (pinned in Dockerfiles + CI; bump in one PR).
- FastAPI **>=0.115,<1**, uvicorn **>=0.32,<1**.
- python-telegram-bot **>=21.6,<22** with extras
  `[rate-limiter, callback-data, job-queue, webhooks]`.
- SQLAlchemy **>=2.0,<3**, asyncpg **>=0.30,<1**, alembic **>=1.14,<2**.
- pgvector Python package **>=0.3,<1**; Postgres image
  `pgvector/pgvector:pg16` (no `pg_cron` at v0 — Q-029 [PROPOSED]
  drops the daily reputation cron in favour of reactive on-outcome
  enqueues. All scheduling is Redis-only via RQ Scheduler `enqueue_at`).
- RQ **>=2.0,<3** + redis **>=5.2,<6**.
- Pydantic **>=2.9,<3**, pydantic-settings **>=2.6,<3**.
- ruff **0.7.4**, mypy via `uv run mypy`, import-linter via
  `uv run lint-imports`.
- pytest **>=8**, pytest-asyncio **>=0.24**, syrupy **>=4.7**,
  hypothesis **>=6.115**, testcontainers **>=4.8**.

## 13. How to add a feature (walkthrough)

Example: "show users their last claim's outcome."

1. **Route** — `apps/api/merlin_api/routes/board.py` adds
   `GET /board/me/latest-outcome` (or extends the existing /me
   route). Routes are flat — no URL versioning.
2. **Schema** — `packages/python/merlin_core/merlin_core/contracts/board.py` adds
   `LatestOutcomeResponse(BaseModel)`.
3. **Use case + port** — `merlin_core.application.use_cases.board` adds
   `get_latest_outcome(source_id)`; its repository dependency is a
   Protocol on `merlin_core.application.ports.OutcomeRepositoryPort`.
4. **Adapter** —
   `packages/python/merlin_infrastructure/merlin_infrastructure/db/repositories/outcome_repository.py`
   implements the new query (append-only — only reads here).
5. **Client** — `merlin_client.client.MerlinClient.latest_outcome()`
   plus tests in `packages/python/merlin_client/tests/`.
6. **OpenAPI** — `make openapi`, review diff, commit.
7. **Bot** — `apps/bot/merlin_bot/handlers/me.py` (or wherever it fits)
   calls `merlin_client.latest_outcome()` and renders MarkdownV2.
8. **Tests** — unit on the use case, integration on the route + repo,
   snapshot on the OpenAPI diff. **Coverage gate stays green.**
9. **Env vars** — none needed for this feature; if it did,
   `Settings` + `.env.example` move together (`make env-check`).
10. **Docs** — if the feature touches DX (new make target, new docker
    flow), update README + ARCHITECTURE.

## 14. What NOT to do (cites MVP §15 invariants)

- **Never UPDATE** rows in `claims` (except the `edits` JSONB +
  `soft_deleted_at` columns), `outcomes`, `source_reputation_history`,
  `attribution_ledger`, or `override_log`. MVP §15 invariants 7/8/10/12.
- **Never log raw Telegram handles.** Use the `source_id` opaque hash
  from `merlin_core.domain.source_id.make_source_id`. MVP §15 invariant 9.
- **Never store `regret_usd`** as a canonical column. It is derived
  (`pnl_if_followed - pnl_if_ignored`). MVP §15 invariant 3.
- **Never bypass the supersedes chain** on outcome corrections. INSERT
  a new outcome row with `supersedes_outcome_id` pointing at the old
  head; do not UPDATE the old row. MVP §15 invariant 6.
- **Never default `pnl_if_ignored_usd` to NULL.** It is `0` until
  resolution sets a real value. MVP §15 invariant 3.
- **Never run integration tests on a non-pgvector Postgres.** The
  custom image at `deploy/postgres-dev/Dockerfile` is used everywhere;
  the upstream `pgvector/pgvector:pg16` alone is the right base. We
  no longer ship `pg_cron` — all scheduling lives in Redis (Q-029).
- **Never commit a secret.** GitHub Push Protection will block; if you
  see the warning, do **not** force-push around it — rotate the secret
  via `fly secrets set` first.
- **Never skip the OpenAPI snapshot commit** after changing a route.
  `make openapi-check` will fail CI; the bot/web frontend will drift.
- **Never use `PicklePersistence`** in the bot prod image. Doesn't
  survive horizontal scaling.
- **Never add a top-level `tests/` directory.** Tests live with the app
  or package they test.
- **Never put Python-specific tooling in the root that isn't shared
  across all Python services** (per the polyglot-monorepo convention —
  see `docs/ARCHITECTURE.md` §4).

## 15. PR ritual

Use the template at `.github/pull_request_template.md`. Required boxes:

- AI disclosure
- CI green
- OpenAPI snapshot regenerated if routes changed
- `.env.example` updated if Settings changed
- Security check (no secrets, no risky new deps)
- Append-only invariants respected
- PII (raw TG handles) not introduced
- Tests added
- Docs updated
