"""HMAC bearer auth — v0 service-to-service token validator.

The bot and worker send `Authorization: Bearer <MERLIN_API_INTERNAL_TOKEN>`.
This module compares the incoming token to the configured value in
constant time (so a timing attack can't enumerate the secret).

Phase 2 adds a JWT validator as a second FastAPI dependency for the
React frontend; this HMAC validator stays for service-to-service.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from merlin_api.config import Settings
from merlin_api.deps import get_settings

BEARER_PREFIX = "Bearer "


def require_internal_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency that rejects requests without a valid bearer token.

    Raises:
        HTTPException 401: missing or malformed Authorization header.
        HTTPException 403: header present but token does not match.
    """
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = authorization[len(BEARER_PREFIX) :]
    expected = settings.merlin_api_internal_token
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal service token.",
        )
