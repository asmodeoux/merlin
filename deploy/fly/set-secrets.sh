#!/usr/bin/env bash
# deploy/fly/set-secrets.sh — push secrets to Fly for merlin-{api,tg-bot,worker}.
#
# Source: apps/<svc>/.env (kept gitignored). The script pulls every
# allow-listed key from each .env, applies three classes of override
# (DATABASE_URL scheme rewrite, MERLIN_API_INTERNAL_TOKEN regeneration,
# REDIS_URL pointing at Upstash + Flycast API hostname for the bot),
# and stages everything via `fly secrets set --stage`. A single
# `fly secrets deploy` at the end recreates each app's machine once.
#
# Inputs (required env vars, never committed):
#   MPG_RAW_DSN   raw `postgresql://...` returned by `fly mpg attach`.
#                 Script rewrites the scheme to `postgresql+asyncpg://`.
#   REDIS_RAW_URL `redis://default:<pwd>@fly-merlin-redis.upstash.io:6379`
#                 returned by `fly redis create`.
#
# Re-runnable: idempotent except for MERLIN_API_INTERNAL_TOKEN, which is
# generated once and cached locally at deploy/fly/.internal-token
# (gitignored). Delete that file to force a rotation — but then re-run
# this script against ALL three apps in one go, or HMAC auth breaks.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FLY_DIR="$REPO_ROOT/deploy/fly"
INTERNAL_TOKEN_FILE="$FLY_DIR/.internal-token"

API_APP="merlin-api"
BOT_APP="merlin-tg-bot"
WORKER_APP="merlin-worker"

# `.internal` (private 6PN, direct to each machine's uvicorn port)
# instead of `.flycast` because Flycast routes through the public-side
# HTTP proxy on ports 80/443 — `http://merlin-api.flycast:8000` would
# never connect. `.internal` resolves to the API machines' 6PN IPs
# directly so the bot's MerlinClient talks to uvicorn on 8000 without
# touching the public TLS hop.
API_INTERNAL_HOST="http://merlin-api.internal:8000"

color_ok="\033[1;32m"
color_fail="\033[1;31m"
color_dim="\033[2m"
color_reset="\033[0m"

ok()   { printf "  ${color_ok}\xE2\x9C\x93${color_reset} %s\n" "$1"; }
fail() { printf "  ${color_fail}\xE2\x9C\x97${color_reset} %s\n" "$1"; exit 1; }
note() { printf "  ${color_dim}\xE2\x80\xA6${color_reset} %s\n" "$1"; }

[[ -n "${MPG_RAW_DSN:-}" ]]   || fail "MPG_RAW_DSN unset (raw postgresql://... from \`fly mpg attach\` output)"
[[ -n "${REDIS_RAW_URL:-}" ]] || fail "REDIS_RAW_URL unset (raw redis://... from \`fly redis create\` output)"

# Rewrite scheme so sqlalchemy picks the asyncpg driver. The
# rest of the DSN (user/password/host/port/db) is preserved verbatim.
# sed used instead of bash ${var/pattern/replacement} because the
# slash-heavy replacement string is awkward to escape inside the
# parameter-expansion delimiter.
ASYNCPG_DSN=$(printf '%s' "$MPG_RAW_DSN" | sed -E 's|^postgres(ql)?://|postgresql+asyncpg://|')
case "$ASYNCPG_DSN" in
  postgresql+asyncpg://*) ;;
  *) fail "MPG_RAW_DSN must start with postgres:// or postgresql:// (got: ${MPG_RAW_DSN%%://*}://)";;
esac
ok "DATABASE_URL scheme rewritten to postgresql+asyncpg://"

# Upstash supports ONLY logical DB 0 (`SELECT 1` returns
# `ResponseError: Only 0th database is supported`). All three apps
# therefore share DB 0:
#   - RQ (worker, api enqueues) — keys `rq:*`
#   - PTB persistence (bot)     — keys `merlin_bot:ptb:*`
# The prefixes are disjoint, so co-tenancy is safe in DB 0.
REDIS_URL_SHARED="$REDIS_RAW_URL"
ok "REDIS_URL pointed at Upstash DB 0 (Upstash doesn't support \`SELECT n\`; PTB and RQ share via key prefix)"

mkdir -p "$FLY_DIR"
if [[ ! -s "$INTERNAL_TOKEN_FILE" ]]; then
  openssl rand -hex 32 > "$INTERNAL_TOKEN_FILE"
  chmod 600 "$INTERNAL_TOKEN_FILE"
  ok "generated MERLIN_API_INTERNAL_TOKEN -> $INTERNAL_TOKEN_FILE"
else
  ok "reusing MERLIN_API_INTERNAL_TOKEN from $INTERNAL_TOKEN_FILE"
fi
INTERNAL_TOKEN="$(cat "$INTERNAL_TOKEN_FILE")"

# Reads a key from a dotenv file, stripping inline ` # comment` tails
# and surrounding quotes. Empty value is returned as empty string.
read_env() {
  local file=$1 key=$2 line value
  line=$(grep -E "^${key}=" "$file" | tail -n1 || true)
  [[ -z "$line" ]] && { echo ""; return; }
  value="${line#${key}=}"
  value="${value%%[[:space:]]#*}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  echo "$value"
}

# Stage a batch of KEY=VALUE pairs (multi-line stdin) via `fly secrets
# import --stage` — the only Fly CLI surface that accepts secrets on
# stdin (avoids shell quoting hell for values with $, &, spaces, etc.).
stage_app() {
  local app=$1 envfile=$2
  shift 2
  local keys=("$@")
  local payload=""
  local count=0
  for key in "${keys[@]}"; do
    local value
    value=$(read_env "$envfile" "$key")
    if [[ -z "$value" ]]; then
      note "$app: skipping $key (empty in $envfile)"
      continue
    fi
    payload+="${key}=${value}"$'\n'
    count=$((count + 1))
  done
  if [[ "$count" -eq 0 ]]; then
    note "$app: no .env-sourced keys to stage"
    return
  fi
  printf '%s' "$payload" | fly secrets import --app "$app" --stage >/dev/null
  ok "$app: staged $count keys from $envfile"
}

# ---------------------------------------------------------------------------
# merlin-api
# ---------------------------------------------------------------------------
API_ENV="$REPO_ROOT/apps/api/.env"
[[ -s "$API_ENV" ]] || fail "missing $API_ENV"

stage_app "$API_APP" "$API_ENV" \
  TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_CHAT_IDS \
  OPENROUTER_API_KEY OPENROUTER_BASE_URL \
  LLM_PRIMARY_MODEL LLM_FALLBACK_MODEL LLM_MONTHLY_USD_BUDGET \
  COINGECKO_API_KEY COINGECKO_BASE_URL \
  SENTRY_DSN SENTRY_TRACES_SAMPLE_RATE \
  API_TITLE API_VERSION

printf 'DATABASE_URL=%s\nREDIS_URL=%s\nMERLIN_API_INTERNAL_TOKEN=%s\nENVIRONMENT=prod\nLOG_LEVEL=INFO\n' \
  "$ASYNCPG_DSN" "$REDIS_URL_SHARED" "$INTERNAL_TOKEN" \
  | fly secrets import --app "$API_APP" --stage >/dev/null
ok "$API_APP: staged DATABASE_URL + REDIS_URL + MERLIN_API_INTERNAL_TOKEN + ENVIRONMENT + LOG_LEVEL"

# ---------------------------------------------------------------------------
# merlin-tg-bot (Fly app slug; the global `merlin-bot` name was taken)
# ---------------------------------------------------------------------------
BOT_ENV="$REPO_ROOT/apps/bot/.env"
[[ -s "$BOT_ENV" ]] || fail "missing $BOT_ENV"

stage_app "$BOT_APP" "$BOT_ENV" \
  TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_CHAT_IDS \
  TELEGRAM_MODE TELEGRAM_WEBHOOK_URL TELEGRAM_WEBHOOK_SECRET \
  MERLIN_API_TIMEOUT_S \
  SENTRY_DSN SENTRY_TRACES_SAMPLE_RATE

printf 'MERLIN_API_BASE_URL=%s\nMERLIN_API_INTERNAL_TOKEN=%s\nREDIS_URL=%s\nENVIRONMENT=prod\nLOG_LEVEL=INFO\n' \
  "$API_INTERNAL_HOST" "$INTERNAL_TOKEN" "$REDIS_URL_SHARED" \
  | fly secrets import --app "$BOT_APP" --stage >/dev/null
ok "$BOT_APP: staged MERLIN_API_BASE_URL (Flycast) + MERLIN_API_INTERNAL_TOKEN + REDIS_URL + ENVIRONMENT + LOG_LEVEL"

# ---------------------------------------------------------------------------
# merlin-worker
# ---------------------------------------------------------------------------
WORKER_ENV="$REPO_ROOT/apps/worker/.env"
[[ -s "$WORKER_ENV" ]] || fail "missing $WORKER_ENV"

stage_app "$WORKER_APP" "$WORKER_ENV" \
  RQ_QUEUES RQ_DEFAULT_TIMEOUT_S \
  COINGECKO_API_KEY COINGECKO_BASE_URL \
  TELEGRAM_BOT_TOKEN \
  SENTRY_DSN SENTRY_TRACES_SAMPLE_RATE

printf 'DATABASE_URL=%s\nREDIS_URL=%s\nMERLIN_API_INTERNAL_TOKEN=%s\nENVIRONMENT=prod\nLOG_LEVEL=INFO\n' \
  "$ASYNCPG_DSN" "$REDIS_URL_SHARED" "$INTERNAL_TOKEN" \
  | fly secrets import --app "$WORKER_APP" --stage >/dev/null
ok "$WORKER_APP: staged DATABASE_URL + REDIS_URL + MERLIN_API_INTERNAL_TOKEN + ENVIRONMENT + LOG_LEVEL"

# ---------------------------------------------------------------------------
# Flush staged secrets — single machine recreate per app instead of
# one per `fly secrets set` call. Releases serially so we never have
# two apps mid-recreate at the same moment (cheap belt-and-braces).
# ---------------------------------------------------------------------------
for app in "$API_APP" "$WORKER_APP" "$BOT_APP"; do
  if fly status --app "$app" --json 2>/dev/null | grep -q '"Status": *"deployed"'; then
    fly secrets deploy --app "$app" >/dev/null
    ok "$app: deployed staged secrets"
  else
    note "$app: app not yet deployed; secrets will apply on first \`fly deploy\` (no \`secrets deploy\` to issue yet)"
  fi
done

printf "\n${color_ok}All secrets staged + applied.${color_reset}\n"
