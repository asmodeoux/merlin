"""``PATCH /me`` — display-name finalisation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UpdateMeRequest(BaseModel):
    """Body of ``PATCH /me``.

    Includes ``tg_user_id`` so the API can look up the calling source
    via ``source_identity``. The bot's HMAC bearer authenticates the
    service, but the API needs the per-user identifier on every
    authenticated call (we have no per-user JWT at v0).
    """

    display_name: str = Field(..., min_length=1, max_length=40)
    tg_user_id: int
