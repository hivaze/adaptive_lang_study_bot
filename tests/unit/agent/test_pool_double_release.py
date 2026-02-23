"""Verify pool double-release is prevented BEFORE semaphore.release()."""

import asyncio

import pytest

from adaptive_lang_study_bot.agent.pool import SessionPool


class TestPoolDoubleReleasePrevention:

    @pytest.mark.asyncio
    async def test_double_release_does_not_grow_interactive_pool(self):
        """Releasing more times than acquiring must not increase the semaphore beyond its initial value."""
        pool = SessionPool.__new__(SessionPool)
        pool._interactive = asyncio.Semaphore(2)
        pool._proactive = asyncio.Semaphore(2)
        pool._interactive_count = 0
        pool._proactive_count = 0

        # Acquire 1 slot
        assert await pool.acquire_interactive() is True
        assert pool.interactive_active == 1

        # Release correctly
        await pool.release_interactive()
        assert pool.interactive_active == 0

        # Spurious release — should NOT grow pool beyond 2
        await pool.release_interactive()

        # Now acquire — should still respect original limit of 2
        results = await asyncio.gather(*(pool.acquire_interactive() for _ in range(5)))
        acquired = sum(1 for r in results if r)
        assert acquired == 2, f"Pool grew beyond initial size: {acquired} slots acquired instead of 2"

        # Cleanup
        for _ in range(acquired):
            await pool.release_interactive()

    @pytest.mark.asyncio
    async def test_double_release_does_not_grow_proactive_pool(self):
        """Same test for proactive pool."""
        pool = SessionPool.__new__(SessionPool)
        pool._interactive = asyncio.Semaphore(2)
        pool._proactive = asyncio.Semaphore(2)
        pool._interactive_count = 0
        pool._proactive_count = 0

        assert await pool.acquire_proactive() is True
        await pool.release_proactive()
        await pool.release_proactive()  # spurious

        results = await asyncio.gather(*(pool.acquire_proactive() for _ in range(5)))
        acquired = sum(1 for r in results if r)
        assert acquired == 2, f"Pool grew beyond initial size: {acquired} slots acquired instead of 2"

        # Cleanup
        for _ in range(acquired):
            await pool.release_proactive()

    @pytest.mark.asyncio
    async def test_release_without_acquire_is_noop(self):
        """Release on a zero-count pool should not corrupt the semaphore."""
        pool = SessionPool.__new__(SessionPool)
        pool._interactive = asyncio.Semaphore(1)
        pool._proactive = asyncio.Semaphore(1)
        pool._interactive_count = 0
        pool._proactive_count = 0

        # Release without prior acquire
        await pool.release_interactive()
        await pool.release_proactive()

        # Pool should still work normally
        assert await pool.acquire_interactive() is True
        assert await pool.acquire_interactive() is False  # only 1 slot
        await pool.release_interactive()
