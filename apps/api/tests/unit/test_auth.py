"""Tests for HMAC bearer auth — missing header → 401, wrong token → 403."""

from __future__ import annotations

import pytest
from fastapi import status
from merlin_api.config import Settings


@pytest.mark.fast
def test_auth_dep_rejects_missing_header() -> None:
    """The dep raises 401 when no Authorization header is present."""
    from fastapi import HTTPException
    from merlin_api.auth import require_internal_token

    with pytest.raises(HTTPException) as excinfo:
        require_internal_token(
            authorization=None,
            settings=Settings(merlin_api_internal_token="x"),
        )
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.fast
def test_auth_dep_rejects_malformed_header() -> None:
    """Header present but missing the ``Bearer `` prefix → 401."""
    from fastapi import HTTPException
    from merlin_api.auth import require_internal_token

    with pytest.raises(HTTPException) as excinfo:
        require_internal_token(
            authorization="Basic abc",
            settings=Settings(merlin_api_internal_token="right-token"),
        )
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.fast
def test_auth_dep_rejects_wrong_token() -> None:
    """The dep raises 403 when the token does not match."""
    from fastapi import HTTPException
    from merlin_api.auth import require_internal_token

    with pytest.raises(HTTPException) as excinfo:
        require_internal_token(
            authorization="Bearer wrong-token",
            settings=Settings(merlin_api_internal_token="right-token"),
        )
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.fast
def test_auth_dep_accepts_correct_token() -> None:
    """Matching token returns None (no exception)."""
    from merlin_api.auth import require_internal_token

    require_internal_token(
        authorization="Bearer right-token",
        settings=Settings(merlin_api_internal_token="right-token"),
    )
