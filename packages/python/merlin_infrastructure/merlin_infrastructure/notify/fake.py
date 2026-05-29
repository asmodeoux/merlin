"""In-memory ``NotifierPort`` for tests.

Captures every notify call so tests can assert on user-facing strings
(crucial for bot-voice regression coverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NotifyCall:
    """One captured notification."""

    tg_user_id: int
    text: str


@dataclass
class FakeNotifier:
    """Records notifications instead of POSTing them."""

    calls: list[NotifyCall] = field(default_factory=list)

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        """Append the (user_id, text) pair to ``calls``."""
        self.calls.append(NotifyCall(tg_user_id=tg_user_id, text=text))
