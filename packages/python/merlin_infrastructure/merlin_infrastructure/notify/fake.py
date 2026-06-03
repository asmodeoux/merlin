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
    """Records notifications instead of POSTing them.

    Set ``raise_on_call`` to a non-``None`` ``Exception`` instance to
    simulate a transport / encoding failure on the next ``notify_user``
    call. The fake re-raises whatever was set, AFTER appending the
    call to ``calls`` so tests can both assert "we tried to notify"
    and "the failure was caught downstream". Default ``None`` keeps
    the historical happy-path behaviour for every existing caller.
    """

    calls: list[NotifyCall] = field(default_factory=list)
    raise_on_call: Exception | None = None

    async def notify_user(self, tg_user_id: int, text: str) -> None:
        """Record the call; raise if the fake is armed to simulate failure."""
        self.calls.append(NotifyCall(tg_user_id=tg_user_id, text=text))
        if self.raise_on_call is not None:
            raise self.raise_on_call
