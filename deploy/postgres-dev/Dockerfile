# Custom Postgres dev image — pgvector + pg_cron on Postgres 16.
#
# The upstream `pgvector/pgvector:pg16` image does NOT ship `pg_cron`,
# but MVP §8.1 requires it for the daily reputation recompute at 00:05
# UTC (MVP §12). This image adds it.
#
# Build:
#   docker build -t merlin-postgres-dev:pg16 deploy/postgres-dev
#
# Usage in docker-compose.dev.yml uses this image; pg_cron is enabled
# via `shared_preload_libraries` in the postgresql.conf below.

FROM pgvector/pgvector:pg16

# Install pg_cron from the official Postgres apt repository.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgresql-16-cron \
        && \
    rm -rf /var/lib/apt/lists/*

# pg_cron requires shared_preload_libraries; ship a minimal postgresql.conf
# fragment + cron.database_name pin.
COPY conf.d/01-pg_cron.conf /etc/postgresql/conf.d/01-pg_cron.conf

# Postgres wants the conf.d directory referenced from its main config.
# We side-step the upstream config by setting POSTGRES_INITDB_ARGS in
# docker-compose; the conf.d fragment is included via the entrypoint
# snippet below.
COPY init.d/01-enable-extensions.sh /docker-entrypoint-initdb.d/01-enable-extensions.sh
RUN chmod +x /docker-entrypoint-initdb.d/01-enable-extensions.sh
