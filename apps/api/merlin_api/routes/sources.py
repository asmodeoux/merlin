"""Source-management routes — ``POST /sources/admin_bootstrap``.

Admin-only bypass for the invite-redemption gate. An admin's first
``/start`` calls this endpoint instead of ``/invites/redeem``; we
INSERT a fresh ``sources`` + ``source_identity`` row marked
``kind='admin'`` and return the admin's opaque ``source_id``.

Idempotent: a second call from the same admin's Telegram id returns
their existing source row.

Why a dedicated endpoint rather than a magic-code shortcut: keeping
the invite redemption path code-only means the bot never has to know
which kind of caller it is — every onboarded user goes through the
same DB rows, just via a different write path. Future audit / GDPR
queries can filter on ``sources.kind``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from merlin_core.contracts import AdminBootstrapRequest, AdminBootstrapResponse
from merlin_core.domain.source_id import make_source_id

from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["sources"])


@router.post(
    "/sources/admin_bootstrap",
    response_model=AdminBootstrapResponse,
    status_code=status.HTTP_200_OK,
    summary="Create an admin's source row without an invite code (admin-only).",
    description=(
        "Idempotent: a second call from the same admin's Telegram id "
        "returns the existing ``source_id``. Non-admin callers (TG id "
        "not in ``TELEGRAM_ADMIN_CHAT_IDS``) 403."
    ),
)
async def admin_bootstrap(
    body: AdminBootstrapRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AdminBootstrapResponse:
    """Create-or-fetch the admin's source row."""
    from merlin_infrastructure.db.repositories import (
        SourceIdentityRepository,
        SourceRepository,
    )

    if body.tg_user_id not in settings.telegram_admin_chat_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not an admin",
        )

    source_repo = SourceRepository(session)
    identity_repo = SourceIdentityRepository(session)

    existing_source_id = await identity_repo.get_source_id_by_tg_user_id(
        body.tg_user_id,
    )
    if existing_source_id is not None:
        existing = await source_repo.get_by_source_id(existing_source_id)
        if existing is not None:
            return AdminBootstrapResponse(
                source_id=existing.source_id,
                display_name_prompt=existing.display_name is None,
            )

    source_id = make_source_id(body.tg_user_id)
    await source_repo.insert(
        source_id=source_id,
        display_name=body.display_name,
        kind="admin",
        consent_version="v0",
    )
    await identity_repo.insert(
        source_id=source_id,
        tg_user_id=body.tg_user_id,
        tg_handle=body.tg_handle,
    )
    await session.commit()

    return AdminBootstrapResponse(
        source_id=source_id,
        display_name_prompt=body.display_name is None,
    )
