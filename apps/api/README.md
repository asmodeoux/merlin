# merlin-api

FastAPI HTTP server. The canonical Merlin `/v1` surface — every other
client (the Telegram bot today, the React frontend in Phase 2, the Pro
Dashboard in Phase 3) hits the same routes.

## Layout

```
apps/api/
├── pyproject.toml
├── Dockerfile         # base / dev / prod targets
├── fly.toml           # [build.args] TARGET=prod
├── .env.example       # every env var the app reads
├── README.md          # this file
├── merlin_api/        # the importable Python package
│   ├── __init__.py
│   ├── py.typed
│   ├── main.py        # 5-line entry point (run_uvicorn)
│   ├── app.py         # FastAPI factory + middleware + /v1 router mount
│   ├── config.py      # pydantic-settings Settings
│   ├── openapi.py     # metadata, tags, HMAC bearer security scheme
│   ├── auth.py        # internal-service-token HMAC bearer validator (v0)
│   ├── deps.py        # FastAPI Depends() factories
│   ├── errors.py      # exception handlers
│   ├── health.py      # /health + /ready (un-versioned)
│   └── routes/        # one module per resource, all mounted under /v1
├── scripts/
│   └── generate_openapi.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    └── snapshot/      # syrupy OpenAPI snapshot
```

## Run

Local hot-reload (in Docker, with debugpy attach on 5678):

```bash
make dev-up
curl localhost:8000/health
open http://localhost:8000/docs
```

Local without Docker (uvicorn direct):

```bash
uv run uvicorn merlin_api.main:app --reload --port 8000
```

## OpenAPI

The schema is committed at `docs/openapi.json` and snapshot-tested. Regenerate after every route change:

```bash
make openapi
git add docs/openapi.json
```

CI fails on undeclared drift (`make openapi-check`).

## Auth (v0)

`Authorization: Bearer <MERLIN_API_INTERNAL_TOKEN>` (HMAC bearer). The
bot and worker each read the same token from their own `.env`. Phase 2
adds JWT for the React frontend; the HMAC path stays for
service-to-service.

## Debug in IDE

- Local: `🚀 Debug API (local uvicorn)` (launch.json)
- Docker: `🎯 Attach to API (Docker debugpy 5678)` (launch.json)
