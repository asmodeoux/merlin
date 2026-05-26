# merlin-bot

Merlin Telegram bot — `python-telegram-bot` v21+ talking to the Merlin
API over HTTP via `merlin_client`.

## Patterns

See `docs/ARCHITECTURE.md` §9 for the full PTB v21 patterns. Highlights:

- `Application` (not `Updater`) as the central object.
- `AIORateLimiter` for the TG 30 msg/s/bot ceiling.
- `Defaults(parse_mode="MarkdownV2", ..., tzinfo=UTC, block=False)`.
- `BasePersistence` — Redis in prod, Dict in dev. Never Pickle in prod.
- `ConversationHandler` with explicit `IntEnum` states for `/alpha`.
- `CallbackQueryHandler` with PTB's typed `CallbackData` (no string parsing).
- `set_my_commands` on startup via `post_init`, with admin-scoped variant.
- `add_error_handler` → Sentry with PII stripped (MVP §15 invariant 9).
- Webhook + secret token in prod, polling in dev.
- `bot_data["merlin_client"]` carries the `MerlinClient` (no globals).

## Forbidden imports (enforced by import-linter)

`sqlalchemy`, `fastapi`, `merlin_core.domain`, `merlin_core.application`,
`merlin_infrastructure`, `merlin_api`, `merlin_worker`. The bot is an
HTTP client. If a handler needs new data, the contract goes through:

1. Add route in `merlin_api.routes.*` (with `response_model` + tests).
2. Add schema in `merlin_core.contracts.*`.
3. Add client method in `merlin_client.MerlinClient`.
4. Regenerate `docs/openapi.json` via `make openapi`.
5. Add bot handler in `merlin_bot.handlers.*` that calls the client.

See AGENTS.md §6.

## Run

```bash
make dev-up              # polling + debugpy 5679
docker compose logs -f merlin-bot
```

## Debug in IDE

- Local: `🤖 Debug Bot (local polling)` (launch.json)
- Docker: `🎯 Attach to Bot (Docker debugpy 5679)` (launch.json)
