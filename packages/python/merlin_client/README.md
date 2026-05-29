# merlin_client

Async httpx SDK for the Merlin API (flat routes, no URL versioning at
v0 — the OpenAPI `info.version` is the canonical version marker). Used
today by `merlin_bot`; future internal Python services (analytics jobs,
batch importers) will use the same client.

Methods (Sprint 1):

- `redeem_invite(code, tg_user_id, tg_handle)` → `POST /invites/redeem`
- `get_me()` / `update_me(display_name)` → `GET` / `PATCH /me`
- `get_board(window)` → `GET /board`
- `search_assets(query)` → `GET /assets/search`
- `resolve_asset(coingecko_id)` → `POST /assets/resolve`
- `draft_claim(text)` → `POST /claims/draft` (no DB writes)
- `submit_claim(payload)` → `POST /claims` (atomic + `enqueue_at`)
- `get_claim(claim_id)` → `GET /claims/{id}`

## Auth

v0 uses an HMAC bearer token. The client reads
`MERLIN_API_INTERNAL_TOKEN` from its caller's `Settings` (typically
`apps/bot/merlin_bot/config.py`) and sends it as
`Authorization: Bearer <token>` on every request.

Phase 2 adds JWT for the React frontend; the client gains a second
constructor for that path without removing the HMAC path.

## Allowed imports (enforced by import-linter)

- `merlin_core.contracts` — wire types
- `httpx` — transport

No SQLAlchemy, no FastAPI, no Telegram, no `merlin_core.domain`, no
`merlin_core.application`, no `merlin_infrastructure`.

## Layout

Conventional two-layer flat layout:

```
packages/python/merlin_client/
├── pyproject.toml
├── README.md
├── tests/
└── merlin_client/
    ├── __init__.py
    ├── py.typed
    ├── client.py
    └── errors.py
```

Hatchling `packages = ["merlin_client"]` — no `sources` rewrites. See
ADR (q) in `docs/ARCHITECTURE.md`.
