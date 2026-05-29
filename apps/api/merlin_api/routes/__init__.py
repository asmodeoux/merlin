"""Per-resource route modules — flat routes (no URL versioning).

Versioning policy (plan §6): the OpenAPI ``info.version`` field is the
canonical API version marker. No ``/v1/`` URL prefix at v0 (Q-027).
When/if a public, partner-facing surface ever lands, a
``v1.include_router`` mount can be added without disturbing the
existing client.

Each module exports a ``router`` that ``merlin_api.app.create_app``
includes directly.

Sprint 1 resources:

- ``invites`` — ``POST /invites/redeem``.
- ``me``     — ``GET /me``, ``PATCH /me``.
- ``board``  — ``GET /board``.
- ``assets`` — ``GET /assets/search``, ``POST /assets/resolve``
  (Q-027 lazy asset resolution).
- ``claims`` — ``POST /claims/draft`` (Q-028 freeform LLM ingest, no
  DB writes), ``POST /claims`` (atomic commit + RQ enqueue),
  ``GET /claims/{id}``.
"""

from merlin_api.routes import assets, board, claims, invites, me

__all__ = ["assets", "board", "claims", "invites", "me"]
