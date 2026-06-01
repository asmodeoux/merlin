"""``BasePersistence`` implementations — Redis for prod/dev, Dict for tests.

Why Redis (not Pickle):

* ``PicklePersistence`` (the PTB stdlib option) writes to a local disk
  file. It survives a single-container restart but breaks the moment we
  scale horizontally — two bot replicas would each maintain their own
  pickle file and silently diverge. We're single-replica today but the
  fix-up cost when we aren't is "rewrite every conversation handler",
  so we eat the Redis dependency now and keep the upgrade path open.
* Redis is already a hard dependency (RQ uses it for the resolution
  queue), so the new adapter doesn't add any operational surface.

Why pickle the payload:

* PTB stores arbitrary Python objects on ``user_data`` / ``chat_data``
  — including the ``ClaimDraft`` dataclass the ``/alpha`` flow carries
  between steps and the typed ``CallbackData`` tuples in the inline
  keyboards. Pickle round-trips those without bespoke serializers and
  matches the on-disk format ``PicklePersistence`` uses. The payloads
  are trusted (we wrote them ourselves), so the standard pickle
  caveats don't apply.

What's persisted, what isn't:

* ``user_data``       — yes. ``/start`` source_id + display_name and
                        ``/alpha`` ClaimDraft across steps live here.
* ``chat_data``       — yes (cheap, future-proof).
* ``callback_data``   — yes. The typed-CallbackData extra round-trips
                        opaque tuples we look up by short tokens.
* ``conversations``   — yes. ``/start`` and ``/alpha`` are
                        ``ConversationHandler``s; without persistence
                        a restart mid-flow strands the user.
* ``bot_data``        — no. ``application.py`` rebuilds the
                        ``MerlinClient`` + admin allowlist inside
                        ``post_init`` on every startup; persisting it
                        would race with that initialisation (see
                        ``test_application_lifecycle.py``).

The Dict fallback is kept for ``environment == "test"`` so unit tests
that call ``build_application(settings)`` don't have to stand up Redis.
"""

from __future__ import annotations

import logging
import pickle
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis
from telegram.ext import BasePersistence, DictPersistence, PersistenceInput
from telegram.ext._utils.types import (
    CDCData,
    ConversationDict,
    ConversationKey,
)

from merlin_bot.config import Settings

if TYPE_CHECKING:
    pass

_log = logging.getLogger("merlin.bot.persistence")

_DEFAULT_KEY_PREFIX = "merlin_bot:ptb"
_DEFAULT_UPDATE_INTERVAL_S = 60.0


class RedisPersistence(BasePersistence[dict[Any, Any], dict[Any, Any], dict[Any, Any]]):
    """Redis-backed ``BasePersistence`` for PTB v21.

    All payloads are pickled under a single key prefix; one Redis key
    per data category (``user_data`` / ``chat_data`` / ``callback_data``)
    plus one per ConversationHandler name (``conv:<name>``). The
    read-modify-write pattern on the multi-row stores is safe at
    single-bot-instance scale (PTB serialises persistence updates
    within one process); it would need ``WATCH`` / ``MULTI`` to be
    safe across replicas, which we'll add when we actually scale out.

    ``bot_data`` storage is disabled in ``store_data`` because
    ``application.py`` rebuilds the runtime singletons in
    ``post_init`` on every startup — persisting them would clobber
    those values with stale ones.
    """

    __slots__ = ("_key_prefix", "_redis")

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        update_interval: float = _DEFAULT_UPDATE_INTERVAL_S,
        redis: Redis | None = None,
    ) -> None:
        """Construct the persistence bound to a Redis instance.

        Args:
            redis_url: URL of the form ``redis://host:port/db`` or
                ``rediss://...`` for TLS. Ignored when ``redis`` is
                passed (the tests do this with ``fakeredis``).
            key_prefix: Namespace used in front of every key so a
                shared Redis can host multiple bots without collisions.
            update_interval: How often PTB calls our flush methods (in
                seconds). Default matches PTB's stdlib persistences.
            redis: Pre-constructed Redis client (for tests / DI).
        """
        super().__init__(
            store_data=PersistenceInput(
                bot_data=False,
                chat_data=True,
                user_data=True,
                callback_data=True,
            ),
            update_interval=update_interval,
        )
        self._key_prefix = key_prefix
        self._redis: Redis = redis if redis is not None else Redis.from_url(redis_url)

    def _k(self, suffix: str) -> str:
        return f"{self._key_prefix}:{suffix}"

    @staticmethod
    def _enc(value: Any) -> bytes:
        return pickle.dumps(value)

    @staticmethod
    def _dec(blob: bytes | None, default: Any) -> Any:
        if blob is None:
            return default
        # Pickle from Redis is *our* data — we wrote it, the network
        # boundary is trusted (internal Redis), and the alternative
        # (JSON) can't round-trip ClaimDraft / CallbackData tuples.
        # See the module docstring for the trust argument.
        return pickle.loads(blob)  # noqa: S301

    # ------------------------------------------------------------------
    # bot_data — disabled via ``store_data`` so PTB never actually calls
    # these, but the BasePersistence ABC requires the methods to exist.
    # ------------------------------------------------------------------

    async def get_bot_data(self) -> dict[Any, Any]:
        """ABC stub — ``bot_data`` is rebuilt in ``post_init``, not stored."""
        return {}

    async def update_bot_data(self, data: dict[Any, Any]) -> None:
        """ABC stub — ``bot_data`` is rebuilt in ``post_init``, not stored."""
        return None

    async def refresh_bot_data(self, bot_data: dict[Any, Any]) -> None:
        """ABC stub — ``bot_data`` is rebuilt in ``post_init``, not stored."""
        return None

    # ------------------------------------------------------------------
    # user_data
    # ------------------------------------------------------------------

    async def get_user_data(self) -> dict[int, dict[Any, Any]]:
        """Load every user's ``user_data`` dict, keyed by ``user_id``."""
        blob = await self._redis.get(self._k("user_data"))
        return self._dec(blob, {})

    async def update_user_data(self, user_id: int, data: dict[Any, Any]) -> None:
        """Merge one user's ``user_data`` slice into the persisted hash."""
        all_data = await self.get_user_data()
        all_data[user_id] = data
        await self._redis.set(self._k("user_data"), self._enc(all_data))

    async def refresh_user_data(self, user_id: int, user_data: dict[Any, Any]) -> None:
        """No-op — we always re-read from Redis on ``get_user_data``."""
        return None

    async def drop_user_data(self, user_id: int) -> None:
        """Remove one user's slice (called on ``Application.drop_user_data``)."""
        all_data = await self.get_user_data()
        if user_id in all_data:
            del all_data[user_id]
            await self._redis.set(self._k("user_data"), self._enc(all_data))

    # ------------------------------------------------------------------
    # chat_data
    # ------------------------------------------------------------------

    async def get_chat_data(self) -> dict[int, dict[Any, Any]]:
        """Load every chat's ``chat_data`` dict, keyed by ``chat_id``."""
        blob = await self._redis.get(self._k("chat_data"))
        return self._dec(blob, {})

    async def update_chat_data(self, chat_id: int, data: dict[Any, Any]) -> None:
        """Merge one chat's ``chat_data`` slice into the persisted hash."""
        all_data = await self.get_chat_data()
        all_data[chat_id] = data
        await self._redis.set(self._k("chat_data"), self._enc(all_data))

    async def refresh_chat_data(self, chat_id: int, chat_data: dict[Any, Any]) -> None:
        """No-op — we always re-read from Redis on ``get_chat_data``."""
        return None

    async def drop_chat_data(self, chat_id: int) -> None:
        """Remove one chat's slice (called on ``Application.drop_chat_data``)."""
        all_data = await self.get_chat_data()
        if chat_id in all_data:
            del all_data[chat_id]
            await self._redis.set(self._k("chat_data"), self._enc(all_data))

    # ------------------------------------------------------------------
    # callback_data
    # ------------------------------------------------------------------

    async def get_callback_data(self) -> CDCData | None:
        """Load the typed-CallbackData cache (None if never persisted)."""
        blob = await self._redis.get(self._k("callback_data"))
        return self._dec(blob, None)

    async def update_callback_data(self, data: CDCData) -> None:
        """Persist the typed-CallbackData cache verbatim."""
        await self._redis.set(self._k("callback_data"), self._enc(data))

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------

    async def get_conversations(self, name: str) -> ConversationDict:
        """Load the persisted state map for one ConversationHandler."""
        blob = await self._redis.get(self._k(f"conv:{name}"))
        return self._dec(blob, {})

    async def update_conversation(
        self,
        name: str,
        key: ConversationKey,
        new_state: object | None,
    ) -> None:
        """Persist one transition; ``new_state=None`` drops the key entirely."""
        convs = await self.get_conversations(name)
        if new_state is None:
            convs.pop(key, None)
        else:
            convs[key] = new_state
        await self._redis.set(self._k(f"conv:{name}"), self._enc(convs))

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """PTB calls this on shutdown — close the Redis connection."""
        try:
            await self._redis.aclose()
        except Exception as e:
            _log.warning("redis aclose during flush failed: %s", e)


def build_persistence(settings: Settings) -> BasePersistence:
    """Pick the right ``BasePersistence`` impl for the current environment.

    * ``test`` → ``DictPersistence`` (in-memory; unit tests don't
      depend on a live Redis).
    * everything else → ``RedisPersistence`` backed by
      ``settings.redis_url``. Dev compose ships Redis on the same
      network as the bot; prod relies on the upstream Redis the worker
      already uses.

    The branch is on the explicit ``environment`` string rather than
    ``redis_url`` so an empty ``REDIS_URL`` in production fails loud
    (bot won't start) instead of silently downgrading to in-memory
    state.
    """
    if settings.environment == "test":
        return DictPersistence()
    return RedisPersistence(settings.redis_url)
