# Merlin Makefile — single command surface for the whole monorepo.
#
# Run `make help` for the catalog. Every CI step, every VS Code task, and
# every dev-loop command goes through here. If it's not in the Makefile,
# it's not part of the workflow.

.DEFAULT_GOAL := help
.PHONY: help install install-frozen lint format type layers test test-unit \
        test-int test-snapshot test-api test-bot test-worker test-core \
        test-infra test-client test-e2e cov cov-html openapi openapi-check env-check \
        deps-audit gitleaks pip-audit dep-review db-migrate db-revision \
        db-reset seed dev dev-up dev-down dev-logs dev-rebuild docker-test \
        dev-verify docs-check ci ci-fast clean

# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nMerlin Makefile\n\n  Usage: \033[36mmake <target>\033[0m\n\n"} \
	     /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 } \
	     /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Install
install: ## Local non-frozen install (lockfile may update).
	uv sync --all-extras

install-frozen: ## Reproducible install (npm-ci equivalent — fails on drift).
	uv sync --frozen --all-extras

##@ Lint / format / types
lint: ## Ruff lint check.
	uv run ruff check apps packages scripts

format: ## Ruff format (writes).
	uv run ruff format apps packages scripts

format-check: ## Ruff format --check (no writes).
	uv run ruff format --check apps packages scripts

type: ## Mypy across the workspace.
	uv run mypy \
		apps/api/merlin_api \
		apps/bot/merlin_bot \
		apps/worker/merlin_worker \
		packages/python/merlin_core/merlin_core \
		packages/python/merlin_infrastructure/merlin_infrastructure \
		packages/python/merlin_client/merlin_client \
		scripts

layers: ## Import-linter — enforce hexagonal layer contracts.
	uv run lint-imports

##@ Tests
test: test-unit test-int test-snapshot ## Run every test suite.

test-unit: ## Unit tests across the workspace.
	uv run pytest -m "not integration and not snapshot" --cov

test-int: ## Integration tests (testcontainers PG + Redis).
	uv run pytest -m integration --cov

test-snapshot: ## Snapshot tests (syrupy — OpenAPI, formatting).
	uv run pytest -m snapshot

# ---------------------------------------------------------------------------
# Per-package coverage gates. Sprint 1 floors per the bot-flow plan; the
# steady-state targets in pyproject.toml (`[tool.coverage.targets]`) are
# tracked but not yet enforced. `make ci` invokes each `test-*` target
# below, so a regression in any single package fails CI precisely.
#
# Floors are set conservatively against the layered test pyramid landing
# this sprint and ramp toward the steady-state targets across sprints.
# ---------------------------------------------------------------------------
COV_FAIL_API = 50
COV_FAIL_BOT = 35
COV_FAIL_WORKER = 35
COV_FAIL_CORE = 70
COV_FAIL_INFRA = 60
COV_FAIL_CLIENT = 65

test-api: ## API tests only.
	uv run pytest apps/api/tests --cov=apps/api/merlin_api --cov-fail-under=$(COV_FAIL_API)

test-bot: ## Bot tests only.
	uv run pytest apps/bot/tests --cov=apps/bot/merlin_bot --cov-fail-under=$(COV_FAIL_BOT)

test-worker: ## Worker tests only.
	uv run pytest apps/worker/tests --cov=apps/worker/merlin_worker --cov-fail-under=$(COV_FAIL_WORKER)

test-core: ## merlin_core tests only.
	uv run pytest packages/python/merlin_core/tests --cov=packages/python/merlin_core/merlin_core --cov-fail-under=$(COV_FAIL_CORE)

test-infra: ## merlin_infrastructure tests only.
	uv run pytest packages/python/merlin_infrastructure/tests --cov=packages/python/merlin_infrastructure/merlin_infrastructure --cov-fail-under=$(COV_FAIL_INFRA)

test-client: ## merlin_client tests only.
	uv run pytest packages/python/merlin_client/tests --cov=packages/python/merlin_client/merlin_client --cov-fail-under=$(COV_FAIL_CLIENT)

test-e2e: ## End-to-end tests (full-flow stub for now).
	uv run pytest tests/e2e --no-cov

##@ Coverage
cov: ## Run all tests with coverage.
	uv run pytest --cov

cov-html: cov ## Run coverage + open the HTML report.
	@command -v open >/dev/null && open htmlcov/index.html || echo "Open htmlcov/index.html manually"

##@ OpenAPI
openapi: ## Regenerate docs/openapi.json from FastAPI.
	uv run python apps/api/scripts/generate_openapi.py

openapi-check: ## Fail if generated openapi differs from committed docs/openapi.json.
	uv run python apps/api/scripts/generate_openapi.py --check

##@ Env / config
env-check: ## Assert every apps/*/.env.example matches its Settings.model_fields.
	uv run python scripts/env_check.py

##@ Security
deps-audit: pip-audit ## Alias.

pip-audit: ## Scan installed deps for known advisories.
	uv export --format requirements-txt --no-hashes --all-extras | uv run pip-audit -r /dev/stdin

dep-review: ## Local Dependency Review simulation (runs the action's diff logic locally).
	@echo "Dependency Review runs in CI on PR diff. Locally: inspect uv.lock changes."

gitleaks: ## Run gitleaks across the working tree.
	@command -v gitleaks >/dev/null || { echo "Install gitleaks: brew install gitleaks"; exit 1; }
	gitleaks detect --source . --redact --verbose

##@ Database
db-migrate: ## Apply migrations to the dev DB.
	cd packages/python/merlin_infrastructure/alembic && uv run alembic upgrade head

db-revision: ## Generate a new autogenerated revision.
	@test -n "$(m)" || { echo "Usage: make db-revision m='your message'"; exit 1; }
	cd packages/python/merlin_infrastructure/alembic && uv run alembic revision --autogenerate -m "$(m)"

db-current: ## Show the current revision.
	cd packages/python/merlin_infrastructure/alembic && uv run alembic current

db-reset: dev-down ## Drop + recreate the dev DB, then migrate. Assets resolve lazily on first claim (Q-026 / Q-027) — no seed step.
	docker compose -f deploy/docker-compose.dev.yml up -d postgres
	@sleep 3
	$(MAKE) db-migrate

##@ Docker / dev loop
dev: dev-up ## Alias for dev-up.

dev-up: ## Bring up dev compose (api/bot/worker + pg + redis, hot reload + debugpy).
	docker compose -f deploy/docker-compose.dev.yml up --build -d
	@echo ""
	@echo "  ✓ API     http://localhost:8000  (debugpy 5678)"
	@echo "  ✓ Bot     polling                (debugpy 5679)"
	@echo "  ✓ Worker  RQ verbose             (debugpy 5680)"
	@echo "  ✓ PG      localhost:5433  (remapped to avoid native PG on 5432)"
	@echo "  ✓ Redis   localhost:6380  (remapped to avoid native Redis on 6379)"
	@echo ""

dev-down: ## Stop dev compose.
	docker compose -f deploy/docker-compose.dev.yml down

dev-logs: ## Tail dev compose logs.
	docker compose -f deploy/docker-compose.dev.yml logs -f

dev-rebuild: ## Force rebuild dev images.
	docker compose -f deploy/docker-compose.dev.yml build --no-cache

docker-test: ## Build prod images + run integration suite (compose.test.yml).
	docker compose -f deploy/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from runner

dev-verify: ## One-button "is the stack alive" — boot compose + poll /health + /ready + log greps.
	@scripts/dev_verify.sh

##@ Docs parity
docs-check: ## rg-based grep gate — fail on forbidden tokens in current docs/code.
	@scripts/docs_check.sh

##@ CI
ci: install-frozen lint format-check type layers openapi-check env-check pip-audit docs-check test-core test-infra test-client test-api test-bot test-worker test-e2e ## Full local CI parity (layered coverage gates).
	@echo ""
	@echo "  ✓ make ci complete"
	@echo ""

ci-fast: install-frozen lint format-check type layers openapi-check env-check docs-check test-unit ## Fast CI (skip integration + snapshot + audit).

##@ Cleanup
clean: ## Remove caches and build artifacts.
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov .coverage .coverage.* build dist
