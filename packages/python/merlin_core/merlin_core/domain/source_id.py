"""Source-ID opacity helper — ARCHITECTURE §16 invariant 9.

Across the platform we identify a Telegram user by an opaque
``source_id = 'tg:' + sha256(...)`` handle. The raw ``tg_user_id`` only
ever lives in the ``source_identity`` table (the PII table, MVP §13);
every other table, every API route, every log line uses ``source_id``.

This module is the **only** place that derives a ``source_id`` from a raw
Telegram identifier. Repositories that need to look up a user by their
Telegram ID accept a ``source_id`` — they do not accept a raw ID and call
this helper themselves.

Stdlib only — no third-party imports.
"""

from __future__ import annotations

import hashlib
from typing import Final

#: Telegram-channel prefix. Future channels (Twitter, Discord) get their
#: own constants here and a parallel helper.
TG_PREFIX: Final[str] = "tg:"

#: Length of the hex digest stored after the channel prefix. 32 hex chars
#: = 128 bits — collision-resistant for the foreseeable future and short
#: enough to read in logs.
HASH_LEN: Final[int] = 32


def make_source_id(tg_user_id: int | str) -> str:
    """Derive an opaque source_id from a raw Telegram user identifier.

    Args:
        tg_user_id: Raw Telegram user ID (int from `update.effective_user.id`)
            or handle (str). Either is acceptable; both stringify to a stable
            value.

    Returns:
        Opaque ``'tg:<hex>'`` identifier safe to cross repository
        boundaries and appear in logs. The same input always produces the
        same output.

    Raises:
        ValueError: If `tg_user_id` is empty or only whitespace.
    """
    raw = str(tg_user_id).strip()
    if not raw:
        raise ValueError("tg_user_id must not be empty")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{TG_PREFIX}{digest[:HASH_LEN]}"


def is_source_id(candidate: str) -> bool:
    """Return True if the string looks like a Merlin source_id.

    Defensive check used by the contracts layer to reject raw Telegram IDs
    leaking through the API surface.
    """
    return candidate.startswith(TG_PREFIX) and len(candidate) == len(TG_PREFIX) + HASH_LEN
