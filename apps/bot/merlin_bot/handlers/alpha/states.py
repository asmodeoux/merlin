"""Conversation states for ``/alpha``.

The freeform-first flow uses ``PREVIEW`` as the central hub. Edit
buttons jump out to the matching step and back to ``PREVIEW``.
"""

from __future__ import annotations

from enum import IntEnum


class AlphaState(IntEnum):
    """All conversation states for the ``/alpha`` flow."""

    AWAIT_TEXT = 1
    PREVIEW = 2
    EDIT_ASSET = 3
    EDIT_DIRECTION = 4
    EDIT_HORIZON = 5
    EDIT_CONVICTION = 6
    EDIT_THESIS = 7
    PICK_ASSET = 8


# user_data keys — central registry so steps don't drift.
USER_KEY_DRAFT = "alpha_draft"  # dict matching ClaimDraftResponse
USER_KEY_ASSET_RESOLUTION = "alpha_asset_resolution"  # dict (AssetResolution)
USER_KEY_CANDIDATES = "alpha_candidates"  # list[dict] from search
