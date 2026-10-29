#!/usr/bin/env bash
# Runs once on first start (the docker-entrypoint-initdb.d contract).
# Creates the pgvector extension; idempotent (`IF NOT EXISTS`).
#
# Q-029 proposal: no ``pg_cron``. Scheduling lives in Redis
# (RQ Scheduler ``enqueue_at``); reputation recompute is reactive on
# outcome write. One less postgres extension to license, deploy, and
# patch.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL
