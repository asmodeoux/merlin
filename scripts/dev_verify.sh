#!/usr/bin/env bash
# scripts/dev_verify.sh — one-button "is the stack alive" check.
#
# Wired into:
#   * `make dev-verify`
#   * `.vscode/tasks.json` → "🚀 Verify Dev Stack (full launch + healthcheck)"
#
# Flow (bot-flow plan §"Sprint 1 acceptance criteria" #2):
#   1. `docker compose up --build -d`
#   2. wait for postgres + redis healthchecks
#   3. poll /health every 1s up to TIMEOUT_S
#   4. curl /ready --fail
#   5. grep bot + worker logs for startup line
#   6. print "Stack ready" with clickable URLs, or last 50 lines of failing
#      service logs and exit 1.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.dev.yml}"
API_URL="${API_URL:-http://localhost:8000}"
TIMEOUT_S="${TIMEOUT_S:-30}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-1}"
BOT_STARTUP_TOKEN="${BOT_STARTUP_TOKEN:-Application started}"
WORKER_STARTUP_TOKEN="${WORKER_STARTUP_TOKEN:-Worker started}"

color_ok="\033[1;32m"
color_fail="\033[1;31m"
color_dim="\033[2m"
color_reset="\033[0m"

ok()   { printf "  ${color_ok}✓${color_reset} %s\n" "$1"; }
fail() { printf "  ${color_fail}✗${color_reset} %s\n" "$1"; }
note() { printf "  ${color_dim}…${color_reset} %s\n" "$1"; }

dump_logs() {
  local service=$1
  printf "\n${color_dim}── last 50 lines of %s logs ──${color_reset}\n" "$service"
  docker compose -f "$COMPOSE_FILE" logs --tail=50 "$service" || true
}

start=$SECONDS

echo "Verifying dev stack…"
note "compose file: $COMPOSE_FILE"

docker compose -f "$COMPOSE_FILE" up --build -d
ok "compose up"

wait_for() {
  local service=$1
  local until_state=$2
  local deadline=$((SECONDS + TIMEOUT_S))
  while (( SECONDS < deadline )); do
    local state
    state=$(docker compose -f "$COMPOSE_FILE" ps --format json "$service" \
      | python3 -c "import json,sys
try:
  data=json.loads(sys.stdin.read() or '{}')
except Exception:
  data={}
if isinstance(data,list):
  data=data[0] if data else {}
print(data.get('Health') or data.get('State') or '')")
    if [[ "$state" == "$until_state" ]]; then
      ok "$service $until_state"
      return 0
    fi
    sleep "$POLL_INTERVAL_S"
  done
  fail "$service never reached state '$until_state'"
  dump_logs "$service"
  exit 1
}

wait_for postgres healthy
wait_for redis healthy

deadline=$((SECONDS + TIMEOUT_S))
until curl -sf "$API_URL/health" >/dev/null; do
  if (( SECONDS >= deadline )); then
    fail "api /health never responded within ${TIMEOUT_S}s"
    dump_logs api
    exit 1
  fi
  sleep "$POLL_INTERVAL_S"
done
ok "api /health"

if ! curl -sf "$API_URL/ready" >/dev/null; then
  fail "api /ready returned non-2xx"
  curl -s "$API_URL/ready" || true
  echo ""
  dump_logs api
  exit 1
fi
ok "api /ready"

deadline=$((SECONDS + TIMEOUT_S))
while ! docker compose -f "$COMPOSE_FILE" logs bot 2>/dev/null | grep -q "$BOT_STARTUP_TOKEN"; do
  if (( SECONDS >= deadline )); then
    fail "bot never logged '$BOT_STARTUP_TOKEN'"
    dump_logs bot
    exit 1
  fi
  sleep "$POLL_INTERVAL_S"
done
ok "bot started polling"

deadline=$((SECONDS + TIMEOUT_S))
while ! docker compose -f "$COMPOSE_FILE" logs worker 2>/dev/null | grep -q "$WORKER_STARTUP_TOKEN"; do
  if (( SECONDS >= deadline )); then
    fail "worker never logged '$WORKER_STARTUP_TOKEN'"
    dump_logs worker
    exit 1
  fi
  sleep "$POLL_INTERVAL_S"
done
ok "worker started"

elapsed=$((SECONDS - start))
echo ""
printf "${color_ok}Stack ready (${elapsed}s)${color_reset}\n"
echo "  api     $API_URL     (debugpy 5678)"
echo "  bot     polling, ready for messages    (debugpy 5679)"
echo "  worker  RQ verbose                (debugpy 5680)"
echo "  pg      localhost:5432"
echo "  redis   localhost:6379"
echo ""
