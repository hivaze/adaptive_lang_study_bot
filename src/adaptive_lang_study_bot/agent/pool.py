import asyncio

from loguru import logger

from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.metrics import SESSION_POOL_ACTIVE


class SessionPool:
    """Semaphore-based concurrency limiter for Claude SDK sessions."""

    def __init__(self) -> None:
        self._interactive = asyncio.Semaphore(
            settings.max_concurrent_interactive_sessions,
        )
        self._proactive = asyncio.Semaphore(
            settings.max_concurrent_proactive_sessions,
        )
        self._interactive_count = 0
        self._proactive_count = 0

    async def acquire_interactive(self) -> bool:
        """Try to acquire an interactive session slot. Non-blocking.

        Safe in asyncio's cooperative model: ``locked()`` and ``acquire()``
        execute without a yield point when the semaphore has available
        slots, so no other coroutine can interleave between the check
        and the acquire.
        """
        if self._interactive.locked():
            return False
        await self._interactive.acquire()
        self._interactive_count += 1
        SESSION_POOL_ACTIVE.labels(type="interactive").set(self._interactive_count)
        logger.debug(
            "Interactive session acquired ({}/{})",
            self._interactive_count,
            settings.max_concurrent_interactive_sessions,
        )
        return True

    async def release_interactive(self) -> None:
        if self._interactive_count <= 0:
            logger.error("Interactive pool double-release detected (count was {})", self._interactive_count)
            return
        self._interactive.release()
        self._interactive_count -= 1
        SESSION_POOL_ACTIVE.labels(type="interactive").set(self._interactive_count)
        logger.debug("Interactive session released")

    async def acquire_proactive(self) -> bool:
        """Try to acquire a proactive session slot. Non-blocking.

        Safe in asyncio's cooperative model — see ``acquire_interactive``.
        """
        if self._proactive.locked():
            return False
        await self._proactive.acquire()
        self._proactive_count += 1
        SESSION_POOL_ACTIVE.labels(type="proactive").set(self._proactive_count)
        logger.debug(
            "Proactive session acquired ({}/{})",
            self._proactive_count,
            settings.max_concurrent_proactive_sessions,
        )
        return True

    async def release_proactive(self) -> None:
        if self._proactive_count <= 0:
            logger.error("Proactive pool double-release detected (count was {})", self._proactive_count)
            return
        self._proactive.release()
        self._proactive_count -= 1
        SESSION_POOL_ACTIVE.labels(type="proactive").set(self._proactive_count)
        logger.debug("Proactive session released")

    @property
    def interactive_active(self) -> int:
        return self._interactive_count

    @property
    def proactive_active(self) -> int:
        return self._proactive_count


# Global instance
session_pool = SessionPool()
