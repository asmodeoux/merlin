# Pull request

## What

<!-- One sentence: what does this PR change? -->

## Why

<!-- Link the issue / spec section / MVP_SPEC reference. -->

## How

<!-- Notable design choices, alternatives considered. -->

## Checklist

- [ ] **AI disclosure** — code generated or assisted by an AI tool? If
  yes, name the tool (Cursor / Copilot / ...) and confirm you reviewed
  the diff line-by-line.
- [ ] **CI green** — ruff, mypy, import-linter, pytest, coverage gates.
- [ ] **OpenAPI** — added/changed routes? Ran `make openapi` and
  committed `docs/openapi.json`.
- [ ] **Env vars** — added/renamed a Settings field? Updated the
  matching `apps/<svc>/.env.example` in the same commit (`make env-check`
  enforces).
- [ ] **Security** — no secrets in code/tests; no new third-party
  dependency without justification; new dependency reviewed in
  `Dependency Review` step.
- [ ] **Append-only invariants** (MVP §15 7/8/10/12) — UPDATE on
  `claims`/`outcomes`/`source_reputation_history`/`attribution_ledger`/
  `override_log` not introduced (or covered by a new repository test +
  migration trigger).
- [ ] **PII** (MVP §15 invariant 9) — no raw Telegram handles in logs /
  Sentry / API responses.
- [ ] **Tests** — new code has unit tests; touched repository code has
  an integration test.
- [ ] **Docs** — README / ARCHITECTURE / RUNBOOK / AGENTS updated where
  the change affects developer experience or operations.

## Screenshots / output

<!-- Optional: paste relevant CLI output, Telegram screenshot, or
  curl example. -->
