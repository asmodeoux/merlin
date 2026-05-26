# merlin-bot Dockerfile — base / dev / prod, mirror of apps/api/Dockerfile.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.12

COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/bot/pyproject.toml apps/bot/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/python/merlin_core/pyproject.toml packages/python/merlin_core/pyproject.toml
COPY packages/python/merlin_infrastructure/pyproject.toml packages/python/merlin_infrastructure/pyproject.toml
COPY packages/python/merlin_client/pyproject.toml packages/python/merlin_client/pyproject.toml

RUN uv sync --frozen --package merlin-bot --no-dev --no-install-project

COPY apps/bot/ apps/bot/
COPY packages/python/merlin_core/ packages/python/merlin_core/
COPY packages/python/merlin_client/ packages/python/merlin_client/

RUN uv sync --frozen --package merlin-bot --no-dev

# --------------------------------------------------------------------------
# dev — adds debugpy on 5679; polling against the real TG API.
# --------------------------------------------------------------------------
FROM base AS dev

RUN uv sync --frozen --package merlin-bot --extra dev

EXPOSE 5679

# `watchmedo auto-restart` restarts the bot on any source change because
# PTB doesn't have an inbuilt reloader.
CMD ["uv", "run", "--package", "merlin-bot", \
     "watchmedo", "auto-restart", \
     "--directory", "/app/apps/bot", \
     "--directory", "/app/packages/python", \
     "--pattern", "*.py", \
     "--recursive", \
     "--", \
     "python", "-m", "debugpy", "--listen", "0.0.0.0:5679", \
     "-m", "merlin_bot.main"]

# --------------------------------------------------------------------------
# prod — non-root, no healthcheck (bot has no HTTP port unless mode=webhook).
# --------------------------------------------------------------------------
FROM base AS prod

RUN groupadd --gid 1000 merlin && \
    useradd --uid 1000 --gid merlin --shell /bin/bash --create-home merlin && \
    chown -R merlin:merlin /app
USER merlin

# When TELEGRAM_MODE=webhook, expose 8080.
EXPOSE 8080

CMD ["uv", "run", "--package", "merlin-bot", "python", "-m", "merlin_bot.main"]
