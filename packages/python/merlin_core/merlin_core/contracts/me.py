"""``PATCH /me`` — display-name finalisation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UpdateMeRequest(BaseModel):
    """Body of ``PATCH /me``."""

    display_name: str = Field(..., min_length=1, max_length=40)
