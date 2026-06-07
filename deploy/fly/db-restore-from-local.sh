#!/usr/bin/env bash
# deploy/fly/db-restore-from-local.sh
#
# One-shot snapshot of the local dev Postgres (docker compose, port
# 5433) -> Fly Managed Postgres. Used during the initial Fly bootstrap
# (plan §1.4) to seed the prod DB with whatever the user has been
# building against locally.
#
# Requires:
#   - `pg_dump` / `pg_restore` v16+ on PATH (brew install postgresql@16
#     and add /opt/homebrew/opt/postgresql@16/bin to PATH if needed).
#   - Local compose stack reachable on localhost:5433 (we bring up the
#     postgres service alone if it isn't already running).
#   - Fly CLI authed and merlin-pg cluster attached.
#
# Inputs (env vars):
#   MPG_RAW_DSN   raw `postgresql://...` from `fly mpg attach` output —
#                 the pgbouncer DSN works for restore (it's the same
#                 backend, just pooled).
#
# Idempotent: pg_restore --clean --if-exists drops + recreates each
# object, so re-running this overwrites whatever was in Fly with the
# current local snapshot. ONLY safe before traffic starts hitting Fly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SNAPSHOT_FILE="$REPO_ROOT/deploy/fly/merlin-snapshot.dump"
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

dump_major=$(pg_dump --version | awk '{print $3}' | cut -d. -f1)
[[ "$dump_major" -ge 16 ]] || fail "pg_dump must be v16+ (got $(pg_dump --version)). brew install postgresql@16 and put its bin/ ahead on PATH."
ok "pg_dump $(pg_dump --version | awk '{print $3}')"

# Bring local postgres up if not already (idempotent).
if ! docker compose -f "$COMPOSE_FILE" ps postgres --status running 2>/dev/null | grep -q postgres; then
  note "starting local postgres (compose service)"
  docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null
  for _ in $(seq 1 20); do
    docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U merlin -d merlin >/dev/null 2>&1 && break
    sleep 1
  done
fi
ok "local postgres ready on localhost:5433"

DATA_FILE="$REPO_ROOT/deploy/fly/merlin-snapshot.data.sql"

# Two-phase migration:
#   - Schema: `alembic upgrade head` (below). The local dev image
#     (pgvector/pgvector:pg16) auto-enables `vector` on first start,
#     so a schema-mode pg_dump would emit `CREATE EXTENSION vector`
#     that Fly MPG's schema_admin can't apply. Alembic is the canonical
#     source anyway.
#   - Data:  pg_dump --data-only --inserts --format=plain. Plain SQL
#     instead of custom format because pg_restore 17 (Homebrew default)
#     emits `SET transaction_timeout = 0` against Fly MPG's PG16 server,
#     which rejects the unknown GUC. psql tolerates pg_dump's emitted
#     SET statements without caring about pg_restore-time injections.
note "pg_dump (--data-only --inserts) -> $DATA_FILE"
pg_dump "$LOCAL_DSN" \
  --no-owner --no-acl \
  --data-only \
  --inserts \
  --exclude-table-data=alembic_version \
  --format=plain \
  --file "$DATA_FILE.raw"

# Strip PG17-pg_dump-emitted SETs that the PG16 server doesn't know
# about. As of PG 17 pg_dump unconditionally emits these even when
# the target is older. Filtering is safe: they are tightening guards
# (timeouts and trigger-firing modes), not data-shaping directives.
sed -E \
  -e '/^SET transaction_timeout/d' \
  "$DATA_FILE.raw" > "$DATA_FILE"
rm -f "$DATA_FILE.raw"
ok "data snapshot written ($(du -h "$DATA_FILE" | awk '{print $1}'))"

# `fly mpg proxy` opens a local 6PN tunnel to the MPG cluster's
# postgres listener (not pgbouncer — bypasses any pooler quirks for
# the restore). We use port 15432 to stay clear of local 5432/5433
# and rewrite MPG_RAW_DSN's host to localhost:15432 for pg_restore.
PROXY_PORT=15432
MPG_CLUSTER_ID="${MPG_CLUSTER_ID:-$(fly mpg list --json 2>/dev/null | python3 -c 'import json,sys; clusters=json.load(sys.stdin); print(next((c["id"] for c in clusters if c.get("name")=="merlin-pg"), ""))')}"
[[ -n "$MPG_CLUSTER_ID" ]] || fail "could not auto-detect MPG cluster id; set MPG_CLUSTER_ID env var (look up via \`fly mpg list\`)"

MPG_HOST=$(echo "$MPG_RAW_DSN" | sed -E 's|^[^@]+@([^/:]+).*|\1|')
MPG_RESTORE_DSN=$(echo "$MPG_RAW_DSN" | sed -E "s|@${MPG_HOST}(:[0-9]+)?|@localhost:${PROXY_PORT}|")

note "opening fly mpg proxy (cluster=$MPG_CLUSTER_ID) on localhost:${PROXY_PORT}"
fly mpg proxy "$MPG_CLUSTER_ID" -p "$PROXY_PORT" >/tmp/fly-proxy-restore.log 2>&1 &
PROXY_PID=$!
trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
# Wait for the proxy port to start accepting before pg_restore.
for _ in $(seq 1 20); do
  nc -z localhost "$PROXY_PORT" 2>/dev/null && break
  sleep 1
done
nc -z localhost "$PROXY_PORT" 2>/dev/null || { cat /tmp/fly-proxy-restore.log; fail "fly mpg proxy never started listening on $PROXY_PORT"; }
ok "proxy listening on localhost:${PROXY_PORT}"

note "alembic upgrade head -> Fly Managed Postgres (creates schema)"
# alembic resolves `script_location = .` against the current working
# directory (not the .ini file's directory) — same trick used by
# `make db-migrate`. cd into the alembic dir before invoking.
#
# PGSSLMODE=disable is required because we're talking to a plain-TCP
# local tunnel (`fly mpg proxy` decrypts behind the scenes). asyncpg's
# default is to attempt TLS; on the localhost socket the handshake
# resets with ConnectionResetError. asyncpg honors libpq's PGSSLMODE
# env var — much cleaner than the DSN-query approach (SQLAlchemy's
# asyncpg dialect routes `?sslmode=` to a kwarg asyncpg.connect
# doesn't accept).
ALEMBIC_DSN="$(printf '%s' "$MPG_RESTORE_DSN" | sed -E 's|^postgres(ql)?://|postgresql+asyncpg://|')"
(
  cd "$REPO_ROOT/packages/python/merlin_infrastructure/alembic"
  PGSSLMODE=disable \
  DATABASE_URL="$ALEMBIC_DSN" \
    uv run --package merlin-infrastructure alembic upgrade head
) || fail "alembic upgrade head failed (see above)"
ok "schema applied via alembic"

# Idempotent re-run guard: this script is for the one-shot seed only.
# If Fly MPG already has rows in any append-only table (sources,
# claims, outcomes), bail rather than crash on PK conflict. The
# append-only triggers (trg_*_no_update / trg_*_no_delete) make
# wholesale overwrite a poor fit for a re-run anyway — fix the local
# DB first and only seed Fly once.
EXISTING_ROWS=$(PGSSLMODE=disable psql \
  --quiet --tuples-only --no-align \
  --dbname "$MPG_RESTORE_DSN" \
  -c "SELECT (SELECT count(*) FROM sources) + (SELECT count(*) FROM claims) + (SELECT count(*) FROM outcomes);" \
  2>/dev/null | tr -d ' ')

if [[ -n "$EXISTING_ROWS" && "$EXISTING_ROWS" != "0" ]]; then
  note "Fly MPG already has $EXISTING_ROWS rows across sources/claims/outcomes — skipping data load (re-runs are blocked to honour append-only invariants)"
else
  note "psql -f $DATA_FILE -> Fly Managed Postgres"
  # psql instead of pg_restore: see two-phase comment above.
  # ON_ERROR_STOP=1 turns a failed INSERT into a non-zero exit,
  # matching the safety pg_restore --exit-on-error gives us.
  PGSSLMODE=disable psql \
    --quiet \
    --set ON_ERROR_STOP=1 \
    --dbname "$MPG_RESTORE_DSN" \
    --file "$DATA_FILE" \
    || fail "psql data load failed (see above)"
  ok "data restored"
fi

printf "\n${color_ok}DB seeded.${color_reset} Next: \`make fly-deploy-api\` (the release_command re-runs \`alembic upgrade head\` as a safety net — no-op on a fresh restore).\n"
