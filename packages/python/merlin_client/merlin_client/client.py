"""Async Merlin API client.

v0 talks to the API over HTTP with an HMAC bearer token (read from the
caller's environment as ``MERLIN_API_INTERNAL_TOKEN``). All routes live
under ``/v1``; ``/health`` and ``/ready`` stay unversioned.

Sprint 1 fills in per-resource methods. Planned surface:

- ``submit_claim(...)``, ``get_claim(claim_id)``
- ``get_board(...)``, ``get_me(source_id)``
- ``redeem_invite(code)``
- ``search_assets(query)`` — backs the bot's `/alpha` asset step.
  Returns CoinGecko candidates (Q-027 lazy asset resolution).
- ``resolve_asset(coingecko_id)`` — INSERT-or-fetch the local
  ``assets`` row, returns ``asset_id``. Called after the user picks
  one of the search candidates.

The scaffold here ships the transport + auth wiring + the error shape so
the bot can be wired against it at the same time the API routes land.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from merlin_client.errors import MerlinHTTPError, MerlinTimeoutError

DEFAULT_TIMEOUT_S: float = 10.0
USER_AGENT: str = "merlin-client/0.1.0"


class MerlinClient:
    """Async HTTP client for the Merlin /v1 API.

    Usage:

        async with MerlinClient(base_url=..., token=...) as client:
            # Sprint 1 — per-resource methods live here.
            ...

    Args:
        base_url: Full base URL of the API (e.g. ``http://merlin-api:8000``
            in dev, ``https://api.merlin.fly.dev`` in prod). The client
            appends ``/v1/<resource>`` for business routes; pass the host
            root, not the ``/v1`` prefix.
        token: HMAC bearer token (``MERLIN_API_INTERNAL_TOKEN``). Sent as
            ``Authorization: Bearer <token>`` on every request.
        timeout: Per-request timeout in seconds. Default 10s.
        transport: Optional custom transport (used in tests via
            ``httpx.MockTransport``).
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct the client. See class docstring for argument semantics."""
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required (set MERLIN_API_INTERNAL_TOKEN)")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        """Enter the async context — return the client unchanged."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the async context — close the underlying httpx transport."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        await self._client.aclose()

    # -----------------------------------------------------------------------
    # Health / readiness — un-versioned probes for the bot's startup check.
    # -----------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Hit ``/health`` (liveness). Returns the API's small status payload."""
        return await self._get("/health")

    async def ready(self) -> dict[str, Any]:
        """Hit ``/ready`` (readiness). Raises ``MerlinHTTPError`` on 503."""
        return await self._get("/ready")

    # -----------------------------------------------------------------------
    # Internal request helpers — Sprint 1 builds per-resource methods on top.
    # -----------------------------------------------------------------------
    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise MerlinTimeoutError(str(exc)) from exc
        return self._unwrap(response)

    async def _post(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=json)
        except httpx.TimeoutException as exc:
            raise MerlinTimeoutError(str(exc)) from exc
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict[str, Any]:
        """Raise on non-2xx, return parsed JSON otherwise."""
        if response.is_success:
            payload = response.json()
            if not isinstance(payload, dict):
                raise MerlinHTTPError(
                    response.status_code,
                    "Unexpected non-object response payload",
                    detail=payload,
                )
            return payload
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise MerlinHTTPError(
            response.status_code,
            response.reason_phrase or "HTTP error",
            detail=detail,
        )
