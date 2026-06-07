#!/usr/bin/env bash
# deploy/fly/db-pull-to-local.sh
#
# Reverse of db-restore-from-local.sh — pull a snapshot of Fly Managed
# Postgres into the local docker compose Postgres so you can reproduce
# a prod bug locally. Plan §2.2.
#
# Not wired into `make dev-up`. Run explicitly via `make
# db-pull-from-fly`.
#
# Safe-by-default: wipes the local volume before restore (so you don't
# end up with a half-merged local/prod state).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SNAPSHOT_FILE="$REPO_ROOT/deploy/fly/merlin-fly-snapshot.dump"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.dev.yml"
LOCAL_DSN="${LOCAL_DSN:-postgres://merlin:merlin@localhost:5433/merlin}"

color_ok="\033[1;32m"
color_fail="\033[1;31m"
color_dim="\033[2m"
color_reset="\033[0m"
ok()   { printf "  ${color_ok}\xE2\x9C\x93${color_reset} %s\n" "$1"; }
fail() { printf "  ${color_fail}\xE2\x9C\x97${color_reset} %s\n" "$1"; exit 1; }
note() { printf "  ${color_dim}\xE2\x80\xA6${color_reset} %s\n" "$1"; }

[[ -n "${MPG_RAW_DSN:-}" ]] || fail "MPG_RAW_DSN unset (raw postgresql://... from \`fly mpg attach\` output)"
command -v pg_dump    >/dev/null || fail "pg_dump not on PATH (brew install postgresql@16)"
command -v pg_restore >/dev/null || fail "pg_restore not on PATH (brew install postgresql@16)"

# Tear down local stack so nothing is mid-write while we restore.
docker compose -f "$COMPOSE_FILE" down >/dev/null

PROXY_PORT=15432
MPG_CLUSTER_ID="${MPG_CLUSTER_ID:-$(fly mpg list --json 2>/dev/null | python3 -c 'import json,sys; clusters=json.load(sys.stdin); print(next((c["id"] for c in clusters if c.get("name")=="merlin-pg"), ""))')}"
[[ -n "$MPG_CLUSTER_ID" ]] || fail "could not auto-detect MPG cluster id; set MPG_CLUSTER_ID env var (look up via \`fly mpg list\`)"

MPG_HOST=$(echo "$MPG_RAW_DSN" | sed -E 's|^[^@]+@([^/:]+).*|\1|')
MPG_DUMP_DSN=$(echo "$MPG_RAW_DSN" | sed -E "s|@${MPG_HOST}(:[0-9]+)?|@localhost:${PROXY_PORT}|")

note "opening fly mpg proxy (cluster=$MPG_CLUSTER_ID) on localhost:${PROXY_PORT}"
fly mpg proxy "$MPG_CLUSTER_ID" -p "$PROXY_PORT" >/tmp/fly-proxy-pull.log 2>&1 &
PROXY_PID=$!
trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  nc -z localhost "$PROXY_PORT" 2>/dev/null && break
  sleep 1
done
nc -z localhost "$PROXY_PORT" 2>/dev/null || { cat /tmp/fly-proxy-pull.log; fail "fly mpg proxy never started listening on $PROXY_PORT"; }

note "pg_dump Fly -> $SNAPSHOT_FILE"
pg_dump "$MPG_DUMP_DSN" \
  --no-owner --no-acl \
  --format=custom \
  --file "$SNAPSHOT_FILE"
ok "snapshot written ($(du -h "$SNAPSHOT_FILE" | awk '{print $1}'))"
kill "$PROXY_PID" 2>/dev/null || true
trap - EXIT

note "starting local postgres + restoring"
docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null
for _ in $(seq 1 20); do
  docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U merlin -d merlin >/dev/null 2>&1 && break
  sleep 1
done

pg_restore \
  --no-owner --no-acl \
  --clean --if-exists \
  --dbname "$LOCAL_DSN" \
  "$SNAPSHOT_FILE"
ok "local DB now mirrors Fly"

printf "\n${color_ok}Done.${color_reset} \`make dev-up\` to bring the rest of the stack back.\n"
