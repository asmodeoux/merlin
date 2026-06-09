"""Async Merlin API client.

Talks to the API over HTTP with an HMAC bearer token (read from the
caller's environment as ``MERLIN_API_INTERNAL_TOKEN``). Routes are
flat — no URL versioning (plan §6); the OpenAPI ``info.version``
field is the canonical version marker, surfaced via ``GET /openapi.json``.

Per-resource methods:

- ``draft_claim(text)`` — Q-028 freeform LLM extraction (stateless).
- ``submit_claim(...)``, ``get_claim(claim_id)`` — atomic commit + inspect.
- ``get_board(window)``, ``get_me()``, ``update_me(display_name)``.
- ``redeem_invite(code, tg_user_id, tg_handle)``,
  ``create_invite(tg_user_id, expires_in_days)``,
  ``revoke_invite(code, tg_user_id)``.
- ``search_assets(query)``, ``resolve_asset(coingecko_id)`` — backs the
  bot's `/alpha` asset step (Q-027 lazy asset resolution).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from merlin_client.errors import MerlinHTTPError, MerlinTimeoutError

DEFAULT_TIMEOUT_S: float = 10.0
USER_AGENT: str = "merlin-client/0.1.0"


class MerlinClient:
    """Async HTTP client for the Merlin API (flat routes, no URL versioning).

    Usage:

        async with MerlinClient(base_url=..., token=...) as client:
            draft = await client.draft_claim("ETH long 14d strong")
            ...

    Args:
        base_url: Full base URL of the API (e.g. ``http://merlin-api:8000``
            in dev, ``https://api.merlin.fly.dev`` in prod). Routes are
            flat: ``/claims``, ``/me``, ``/board``, ``/assets/...``.
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
    # Invites
    # -----------------------------------------------------------------------
    async def redeem_invite(
        self,
        *,
        code: str,
        tg_user_id: int,
        tg_handle: str | None = None,
    ) -> dict[str, Any]:
        """``POST /invites/redeem`` — atomic invite consumption."""
        return await self._post(
            "/invites/redeem",
            json={
                "code": code,
                "tg_user_id": tg_user_id,
                "tg_handle": tg_handle,
            },
        )

    async def create_invite(
        self,
        *,
        tg_user_id: int,
        expires_in_days: int = 14,
    ) -> dict[str, Any]:
        """``POST /invites`` — issue a new invite code.

        Open to any onboarded source — the only authorisation gate on
        the server is "your ``tg_user_id`` must already resolve to a
        ``sources`` row" (i.e. you've finished /start). Returns
        ``{code, expires_at}``. 403s with ``not_a_source`` if the
        caller hasn't completed onboarding yet.
        """
        return await self._post(
            "/invites",
            json={
                "tg_user_id": tg_user_id,
                "expires_in_days": expires_in_days,
            },
        )

    async def revoke_invite(
        self,
        *,
        code: str,
        tg_user_id: int,
    ) -> dict[str, Any]:
        """``POST /invites/revoke`` — admin-only invite invalidation.

        Returns ``{code, revoked_at}`` on success. 403s if ``tg_user_id``
        isn't in the API's admin allowlist; 404s if the code is unknown,
        already redeemed, or already revoked.
        """
        return await self._post(
            "/invites/revoke",
            json={
                "code": code,
                "tg_user_id": tg_user_id,
            },
        )

    # -----------------------------------------------------------------------
    # Sources / admin
    # -----------------------------------------------------------------------
    async def admin_bootstrap(
        self,
        *,
        tg_user_id: int,
        tg_handle: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """``POST /sources/admin_bootstrap`` — admin onboarding without an invite.

        Idempotent. Returns ``{source_id, display_name_prompt}``.
        """
        return await self._post(
            "/sources/admin_bootstrap",
            json={
                "tg_user_id": tg_user_id,
                "tg_handle": tg_handle,
                "display_name": display_name,
            },
        )

    # -----------------------------------------------------------------------
    # Sources / me
    # -----------------------------------------------------------------------
    async def get_me(self, *, tg_user_id: int) -> dict[str, Any]:
        """``GET /me?tg_user_id=...`` — reputation snapshot.

        ``tg_user_id`` rides the query string for the same reason
        ``update_me`` puts it in the body: the HMAC bearer
        authenticates the *service*, not the per-user identity, so the
        API needs the per-user identifier on every authenticated call.
        """
        return await self._get("/me", params={"tg_user_id": tg_user_id})

    async def get_my_tickers(self, *, tg_user_id: int) -> dict[str, Any]:
        """``GET /me/tickers?tg_user_id=...`` — per-ticker breakdown.

        Returns ``{"entries": [...]}``. Each entry carries the user-
        facing fields (asset_symbol, accuracy_pct, weighted_pnl_pct,
        calls_resolved, calls_correct); the internal Bayesian-shrunk
        sort score that drove the row order is NOT on the wire.
        Sorted server-side; the bot renders the list in receive order.
        """
        return await self._get("/me/tickers", params={"tg_user_id": tg_user_id})

    async def get_my_history(
        self,
        *,
        tg_user_id: int,
        limit: int = 30,
        offset: int = 0,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """``GET /me/history`` — newest-first resolved-call history.

        Returns ``{"entries": [...], "has_more": bool, "next_offset":
        int | None, "as_of": str}``. Newest-first order is contract;
        the bot renders rows in receive order. ``has_more`` +
        ``next_offset`` let the bot decide whether to render the
        "Show 30 more" inline button without keeping its own count.

        ``as_of`` freezes the pagination window:

        * First page: omit (``None``). Server stamps the response
          with its UTC wall-clock value and echoes it back.
        * Follow-up pages: pass the value the server returned on
          page 1. Server filters ``WHERE resolved_at <= as_of`` so a
          new outcome resolving mid-session can't shift already-
          paged rows down into the next page (which would dup the
          boundary row on the next "Show 30 more" click).

        Pass ``as_of`` as an ISO 8601 string (``2026-04-28T12:00:00Z``).
        """
        params: dict[str, Any] = {
            "tg_user_id": tg_user_id,
            "limit": limit,
            "offset": offset,
        }
        if as_of is not None:
            params["as_of"] = as_of
        return await self._get("/me/history", params=params)

    async def update_me(self, *, display_name: str, tg_user_id: int) -> None:
        """``PATCH /me`` — set leaderboard display name. Returns 204.

        ``tg_user_id`` is included in the body so the API can look the
        calling source up via the ``source_identity`` table — the HMAC
        bearer authenticates the *service*, not the per-user identity.
        """
        try:
            response = await self._client.patch(
                "/me",
                json={"display_name": display_name, "tg_user_id": tg_user_id},
            )
        except httpx.TimeoutException as exc:
            raise MerlinTimeoutError(str(exc)) from exc
        if not response.is_success:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise MerlinHTTPError(
                response.status_code,
                response.reason_phrase or "HTTP error",
                detail=detail,
            )

    # -----------------------------------------------------------------------
    # Board
    # -----------------------------------------------------------------------
    async def get_board(
        self,
        *,
        window: str = "all_time",
        horizon: str = "all",
    ) -> dict[str, Any]:
        """``GET /board`` — leaderboard for the window + horizon filter.

        ``horizon`` is the four-mode "Day Traders" / "Swing Traders"
        / "Position Traders" knob exposed by the bot's inline-button
        row. Accepted values: ``"all"`` (default — pre-aggregated
        snapshot read), ``"day"`` (≤24h bets only), ``"swing"`` (1d-3wk
        bets), ``"position"`` (>3wk bets only). The API rejects
        anything else with a 422.
        """
        return await self._get(
            "/board",
            params={"window": window, "horizon": horizon},
        )

    # -----------------------------------------------------------------------
    # Assets (Q-027 lazy resolution)
    # -----------------------------------------------------------------------
    async def search_assets(self, *, query: str) -> dict[str, Any]:
        """``GET /assets/search`` — candidates for a freeform query."""
        return await self._get("/assets/search", params={"q": query})

    async def resolve_asset(self, *, coingecko_id: str) -> dict[str, Any]:
        """``POST /assets/resolve`` — INSERT-or-fetch by coingecko_id."""
        return await self._post(
            "/assets/resolve",
            json={"coingecko_id": coingecko_id},
        )

    # -----------------------------------------------------------------------
    # Claims (Q-028 freeform LLM draft + atomic commit)
    # -----------------------------------------------------------------------
    async def draft_claim(self, *, text: str) -> dict[str, Any]:
        """``POST /claims/draft`` — freeform LLM extraction; no DB writes."""
        return await self._post("/claims/draft", json={"text": text})

    async def submit_claim(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /claims`` — atomic commit + scheduled resolution.

        ``payload`` follows the ``SubmitClaimRequest`` Pydantic schema
        in ``merlin_core.contracts.claims``. The bot composes this
        from the preview state.
        """
        return await self._post("/claims", json=payload)

    async def get_claim(self, *, claim_id: str) -> dict[str, Any]:
        """``GET /claims/{id}`` — single claim shape (user-visible only)."""
        return await self._get(f"/claims/{claim_id}")

    # -----------------------------------------------------------------------
    # Internal request helpers
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
