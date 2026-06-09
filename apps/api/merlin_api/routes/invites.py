"""Invite routes — ``POST /invites/redeem`` + ``POST /invites`` + ``POST /invites/revoke``.

* ``POST /invites/redeem`` — user pastes a code; the API validates it
  against the ``invites`` table, INSERTs a fresh ``sources`` +
  ``source_identity`` row, and marks the invite redeemed. The whole
  thing is one transaction; two racing redeems for the same code can
  never both succeed (conditional UPDATE in the repository).

* ``POST /invites`` — **any onboarded source** can mint a fresh code.
  The only gate is "the caller must already exist as a ``sources``
  row" — i.e. you can't mint an invite for yourself before you've
  been let into the network. Admins satisfy this gate automatically
  (the bot's ``admin_bootstrap`` flow runs on first ``/start``);
  invited users satisfy it the moment they redeem their own invite.
  Unknown callers 403 with a clear "join first" detail so the bot
  can render the right onboarding nudge.

* ``POST /invites/revoke`` — **admin-only**, kept gated on
  ``Settings.telegram_admin_chat_ids``. Revocation is a moderation
  tool (kill a leaked code) and there's no per-user ownership
  semantic on ``invites.created_by_source_id`` that would let us
  safely scope this to "you can only revoke codes you minted" — a
  follow-up if that ever becomes a product need.

The "random code accept" hole the user flagged is now closed: a code
that doesn't exist in the ``invites`` table 404s, no matter how
plausible the bytes look.

Historical note: ``POST /invites`` was admin-only at v0. The gate
was removed once invite-gated onboarding was robust enough that
"only admins can mint codes" became a growth bottleneck rather than
a safety property. The sybil-resistance argument for the original
admin-only design now lives entirely in "you have to be invited to
mint invites" (chain-of-trust) rather than in a server-side
allowlist — see Q-030 [PROPOSED] in DECISIONS for the quota-based
follow-up plan if the chain alone proves insufficient.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from merlin_core.application.ports import InviteAlreadyRedeemedError
from merlin_core.contracts import (
    CreateInviteRequest,
    CreateInviteResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RevokeInviteRequest,
    RevokeInviteResponse,
)
from merlin_core.domain.source_id import make_source_id

from merlin_api.config import Settings
from merlin_api.deps import get_session, get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(tags=["invites"])


def _generate_invite_code() -> str:
    """Mint a fresh invite code.

    ``secrets.token_urlsafe(8)`` returns ~11 chars (8 bytes encoded as
    URL-safe base64). ~64 bits of entropy — enough to make code-guessing
    pointless for our threat model (we're not a public service yet) while
    staying short enough to paste from a chat.
    """
    return secrets.token_urlsafe(8)


@router.post(
    "/invites/redeem",
    response_model=RedeemInviteResponse,
    status_code=status.HTTP_200_OK,
    summary="Redeem an invite code (atomic — INSERT sources + UPDATE invites).",
    description=(
        "Single transaction: validate invite, INSERT ``sources`` "
        "(zeroed Admiralty: ``reliability='F'``, ``credibility=6``), "
        "INSERT ``source_identity``, UPDATE ``invites`` redeemed. "
        "Returns the new ``source_id`` and a hint to prompt for "
        "display name. Unknown / expired / already-redeemed codes 404."
    ),
)
async def redeem_invite(
    body: RedeemInviteRequest,
    session: AsyncSession = Depends(get_session),
) -> RedeemInviteResponse:
    """Look up the invite and create the source row atomically."""
    from merlin_infrastructure.db.repositories import (
        InviteRepository,
        SourceIdentityRepository,
        SourceRepository,
    )

    invite_repo = InviteRepository(session)
    source_repo = SourceRepository(session)
    identity_repo = SourceIdentityRepository(session)

    # Idempotent re-redeem: same TG user pasting the same code returns
    # their existing source row. We detect this BEFORE touching the
    # invite so a re-redeem doesn't burn a fresh code.
    existing_source_id = await identity_repo.get_source_id_by_tg_user_id(
        body.tg_user_id,
    )
    if existing_source_id is not None:
        existing = await source_repo.get_by_source_id(existing_source_id)
        # ``existing`` is None only if the DB is in an inconsistent state —
        # treat that as the "create from scratch" path and let the FK
        # constraint surface the issue.
        if existing is not None:
            return RedeemInviteResponse(
                source_id=existing.source_id,
                display_name_prompt=existing.display_name is None,
            )

    invite = await invite_repo.get_redeemable_by_code(body.code)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invite code not found or already redeemed",
        )

    source_id = make_source_id(body.tg_user_id)
    await source_repo.insert(
        source_id=source_id,
        display_name=None,
        kind="user",
        consent_version="v0",
    )
    await identity_repo.insert(
        source_id=source_id,
        tg_user_id=body.tg_user_id,
        tg_handle=body.tg_handle,
    )
    claimed = await invite_repo.mark_redeemed(
        invite_id=invite.id,
        redeemed_by_source_id=source_id,
        redeemed_at=datetime.now(tz=UTC),
    )
    if not claimed:
        # Lost the race to another redeemer. Explicitly roll back the
        # source + identity INSERTs we just did — the registered
        # exception handler turns the raise below into a Response
        # *before* the ``yield_session`` dep's except-clause sees the
        # exception, so the dep-level rollback never fires on this
        # path. Without this explicit call we'd leak a half-onboarded
        # source row (an integration test in test_invite_flow.py pins
        # this).
        await session.rollback()
        raise InviteAlreadyRedeemedError(
            f"invite {invite.code!r} consumed by another redeemer",
        )

    await session.commit()
    return RedeemInviteResponse(source_id=source_id, display_name_prompt=True)


@router.post(
    "/invites",
    response_model=CreateInviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new invite code (open to any onboarded source).",
    description=(
        "Generates a URL-safe ~11-character code and stores it with an "
        "expiry. Open to any caller whose ``tg_user_id`` resolves to "
        "an existing ``sources`` row (i.e. they've completed onboarding "
        "via ``/invites/redeem`` or ``admin_bootstrap``). Callers "
        "without a source row 403 with ``not_a_source`` — the bot "
        "renders a 'send /start first' nudge on that path."
    ),
)
async def create_invite(
    body: CreateInviteRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateInviteResponse:
    """Mint a new invite, persist it, return the code.

    No ``Settings`` dep on the signature: the open-invites rollout
    lifted the admin-allowlist check, which was the only consumer of
    settings on this route. Re-add the dep here if a future gate needs
    config (e.g. per-source rate limit, blocked-list, etc.).
    """
    from merlin_infrastructure.db.repositories import InviteRepository

    issuer_source_id = make_source_id(body.tg_user_id)
    if not await _source_exists(session, issuer_source_id):
        # Open to anyone WHO HAS JOINED. Calling /invite before /start
        # is the only way to hit this in normal flow — the bot turns
        # this 403 into a "send /start first" nudge so the user
        # understands the remediation in one tap.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_a_source",
        )

    expires_at = datetime.now(tz=UTC) + timedelta(days=body.expires_in_days)
    invite_repo = InviteRepository(session)

    # Retry on the (vanishingly unlikely) UNIQUE(code) collision. Three
    # attempts is plenty — token_urlsafe(8) has ~64 bits of entropy.
    last_error: Exception | None = None
    for _ in range(3):
        try:
            invite = await invite_repo.insert(
                code=_generate_invite_code(),
                created_by_source_id=issuer_source_id,
                expires_at=expires_at,
            )
            await session.commit()
            return CreateInviteResponse(code=invite.code, expires_at=invite.expires_at)
        except ValueError as e:
            await session.rollback()
            last_error = e
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"could not mint a unique invite code after 3 attempts: {last_error}",
    )


async def _source_exists(session: AsyncSession, source_id: str) -> bool:
    """Cheap presence check: does the caller already have a sources row?

    Used by ``POST /invites`` as the single authorisation gate. Under
    the open-invites policy a missing source row is the ONLY reason
    the route 403s — there's no admin allowlist short-circuit above
    this check anymore (that gate is what we removed).
    """
    from merlin_infrastructure.db.models import Source

    row = await session.get(Source, source_id)
    return row is not None


@router.post(
    "/invites/revoke",
    response_model=RevokeInviteResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke an un-redeemed invite code (admin-only).",
    description=(
        "Marks the invite as revoked so future ``POST /invites/redeem`` "
        "calls 404 against it. Atomic — two simultaneous revoke calls "
        "for the same code can never both succeed. Returns 404 if the "
        "code is unknown, already redeemed, or already revoked (the "
        "three indistinguishable terminal states from a revoke-caller's "
        "perspective). Caller must be a Telegram admin "
        "(``TELEGRAM_ADMIN_CHAT_IDS``); non-admin callers 403."
    ),
)
async def revoke_invite(
    body: RevokeInviteRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RevokeInviteResponse:
    """Atomically revoke an un-redeemed invite by code."""
    from merlin_infrastructure.db.repositories import InviteRepository

    if body.tg_user_id not in settings.telegram_admin_chat_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not an admin",
        )

    invite_repo = InviteRepository(session)
    revoked_at = datetime.now(tz=UTC)
    claimed = await invite_repo.revoke_by_code(
        code=body.code,
        revoked_at=revoked_at,
    )
    if not claimed:
        # Unknown / already-redeemed / already-revoked all collapse to
        # the same 404 — from the admin's point of view, the code is
        # no longer revokable for any of these reasons and the
        # distinction doesn't change the next action ("try a different
        # code" or "this code is already dead, you're done").
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invite code not found or already consumed",
        )

    await session.commit()
    return RevokeInviteResponse(code=body.code, revoked_at=revoked_at)
