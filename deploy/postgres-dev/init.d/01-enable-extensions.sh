#!/usr/bin/env bash
# Runs once on first start (the docker-entrypoint-initdb.d contract).
# Creates the pgvector + pg_cron extensions; idempotent (`IF NOT EXISTS`).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_cron;
EOSQL
