# merlin-api Dockerfile — three targets:
#   - base : python:3.12-slim + uv + frozen deps
#   - dev  : adds dev extras + uvicorn --reload + debugpy
#   - prod : non-root + HEALTHCHECK + signal-handling entry
#
# Build args:
#   TARGET=prod (default; pinned by fly.toml [build.args])
# Build:
#   docker build --target prod -t merlin-api:prod .

# --------------------------------------------------------------------------
# base — frozen install, no dev deps, runtime user not yet switched.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.12

# uv installed via the official binary — pinned via the digest on a real
# build. Slim layer; no apt deps needed for FastAPI itself.
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /usr/local/bin/uv

WORKDIR /app

# Copy the workspace metadata first so dependency-only layers cache well.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/bot/pyproject.toml apps/bot/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY packages/python/merlin_core/pyproject.toml packages/python/merlin_core/pyproject.toml
COPY packages/python/merlin_infrastructure/pyproject.toml packages/python/merlin_infrastructure/pyproject.toml
COPY packages/python/merlin_client/pyproject.toml packages/python/merlin_client/pyproject.toml

# Frozen install — fails the build if uv.lock is stale.
RUN uv sync --frozen --package merlin-api --no-dev --no-install-project

# Now copy source. Workspace members live at fixed paths.
COPY apps/api/ apps/api/
COPY packages/python/merlin_core/ packages/python/merlin_core/
COPY packages/python/merlin_infrastructure/ packages/python/merlin_infrastructure/
COPY packages/python/merlin_client/ packages/python/merlin_client/

# Install the project itself (now that source is present).
RUN uv sync --frozen --package merlin-api --no-dev

# --------------------------------------------------------------------------
# dev — adds dev extras, uvicorn --reload, debugpy on 5678.
# --------------------------------------------------------------------------
FROM base AS dev

RUN uv sync --frozen --package merlin-api --extra dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000 5678

# debugpy listens on 5678; uvicorn reloads on source changes mounted in.
CMD ["uv", "run", "--package", "merlin-api", \
     "python", "-m", "debugpy", "--listen", "0.0.0.0:5678", \
     "-m", "uvicorn", "merlin_api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", \
     "--reload-dir", "/app/apps/api", \
     "--reload-dir", "/app/packages/python"]

# --------------------------------------------------------------------------
# prod — non-root, HEALTHCHECK, plain entry point.
# --------------------------------------------------------------------------
FROM base AS prod

RUN groupadd --gid 1000 merlin && \
    useradd --uid 1000 --gid merlin --shell /bin/bash --create-home merlin && \
    chown -R merlin:merlin /app
USER merlin

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status == 200 else 1)" || exit 1

# tini-style signal forwarding is handled by uvicorn natively under uv.
CMD ["uv", "run", "--package", "merlin-api", "python", "-m", "merlin_api.main"]
