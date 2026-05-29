# Custom Postgres dev image — pgvector on Postgres 16.
#
# We use the upstream ``pgvector/pgvector:pg16`` image directly; the
# only customisation is enabling the ``vector`` extension on first
# start via ``init.d/01-enable-extensions.sh``.
#
# Build:
#   docker build -t merlin-postgres-dev:pg16 deploy/postgres-dev
#
# Q-029 proposal (plan §4): no ``pg_cron``. Scheduling lives in
# Redis (RQ Scheduler ``enqueue_at``) and reputation recompute is
# reactive on outcome. One less postgres extension, simpler image,
# fewer Fly machine permissions.

FROM pgvector/pgvector:pg16

# Enable the vector extension on first start. Idempotent.
COPY init.d/01-enable-extensions.sh /docker-entrypoint-initdb.d/01-enable-extensions.sh
RUN chmod +x /docker-entrypoint-initdb.d/01-enable-extensions.sh
