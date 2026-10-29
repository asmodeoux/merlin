# merlin-worker Dockerfile — base / dev / prod, mirror of apps/api/Dockerfile.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.12

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /app

# Workspace metadata + READMEs (Hatchling needs the README path
# resolvable at metadata-resolution time — see api/Dockerfile for the
# full rationale).
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/README.md apps/api/
COPY apps/bot/pyproject.toml apps/bot/README.md apps/bot/
COPY apps/worker/pyproject.toml apps/worker/README.md apps/worker/
COPY packages/python/merlin_core/pyproject.toml packages/python/merlin_core/README.md packages/python/merlin_core/
COPY packages/python/merlin_infrastructure/pyproject.toml packages/python/merlin_infrastructure/README.md packages/python/merlin_infrastructure/
COPY packages/python/merlin_client/pyproject.toml packages/python/merlin_client/README.md packages/python/merlin_client/

RUN uv sync --frozen --package merlin-worker --no-dev --no-install-project

COPY apps/worker/ apps/worker/
COPY packages/python/merlin_core/ packages/python/merlin_core/
COPY packages/python/merlin_infrastructure/ packages/python/merlin_infrastructure/

RUN uv sync --frozen --package merlin-worker --no-dev

# --------------------------------------------------------------------------
# dev — debugpy on 5680; RQ verbose.
# --------------------------------------------------------------------------
FROM base AS dev

RUN uv sync --frozen --package merlin-worker --extra dev

EXPOSE 5680

CMD ["uv", "run", "--package", "merlin-worker", \
     "watchmedo", "auto-restart", \
     "--directory", "/app/apps/worker", \
     "--directory", "/app/packages/python", \
     "--pattern", "*.py", \
     "--recursive", \
     "--", \
     "python", "-m", "debugpy", "--listen", "0.0.0.0:5680", \
     "-m", "merlin_worker.main"]

# --------------------------------------------------------------------------
# prod — non-root.
# --------------------------------------------------------------------------
FROM base AS prod

RUN groupadd --gid 1000 merlin && \
    useradd --uid 1000 --gid merlin --shell /bin/bash --create-home merlin && \
    chown -R merlin:merlin /app
USER merlin

CMD ["uv", "run", "--package", "merlin-worker", "python", "-m", "merlin_worker.main"]
