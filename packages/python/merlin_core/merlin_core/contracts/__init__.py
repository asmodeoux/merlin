"""Wire contracts — Pydantic schemas shared between `merlin_api` and `merlin_client`.

These are the *only* schemas the API surfaces and the *only* schemas the
Python SDK accepts/returns. The TypeScript client (Phase 2 React frontend)
regenerates from `docs/openapi.json` via `openapi-typescript` and stays in
sync the same way.

Layer rule (enforced by import-linter): this module imports stdlib +
pydantic only. No domain types leak across this boundary — anything that
needs to cross becomes a contract DTO here.
"""
