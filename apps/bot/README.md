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

## `/alpha` — freeform-first

The `/alpha` command (and *any* non-command text message — Q-028
PROPOSED) sends the user's words to `POST /claims/draft`. The API runs
a single LLM call (`temperature=0`, JSON-schema constrained,
OpenRouter), comes back with a prefilled `ClaimDraft`, and the bot
renders the preview message + Confirm / Edit / Cancel keyboard.

Edits jump to the matching button step (`step_direction`,
`step_horizon`, `step_conviction`, `step_thesis`, `step_asset`) and
flow back to the preview with the field overwritten. Confirm composes
`SubmitClaimRequest` and calls `POST /claims`.

**Fallback path.** If `POST /claims/draft` 5xxs, OpenRouter is
rate-limited, or `LLM_MONTHLY_USD_BUDGET` is exhausted, the bot drops
into the deterministic button flow starting at the asset step. The
user sees no "fallback" message — the bot just asks "Which asset?" as
if that were the intended first step. LLM is UX acceleration, never
a system dependency.

**Bot voice.** All user-facing strings follow the bot-voice rules in
`docs/MVP_SPEC.md` §15 invariant 16: no
version markers, no internal data-model nouns, no raw ISO timestamps,
no enum literals, no limitation hedges.

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
