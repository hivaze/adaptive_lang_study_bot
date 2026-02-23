"""Race-condition tests for concurrency-sensitive code paths.

Tests verify that concurrent access produces correct results,
using asyncio.gather to simulate parallel coroutine execution.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adaptive_lang_study_bot.agent.pool import SessionPool


# ---------------------------------------------------------------------------
# cache/client.py — get_redis() concurrent initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_redis_concurrent_init_creates_single_pool():
    """Concurrent get_redis() calls should create exactly one ConnectionPool."""
    import adaptive_lang_study_bot.cache.client as client_mod

    # Save originals and reset module state
    orig_pool, orig_redis, orig_lock = client_mod._pool, client_mod._redis, client_mod._lock
    client_mod._pool = None
    client_mod._redis = None
    client_mod._lock = asyncio.Lock()

    pool_create_count = 0

    class FakePool:
        pass

    class FakeRedis:
        def __init__(self, connection_pool):
            self.connection_pool = connection_pool

    def fake_from_url(*args, **kwargs):
        nonlocal pool_create_count
        pool_create_count += 1
        return FakePool()

    try:
        with (
            patch.object(client_mod.ConnectionPool, "from_url", side_effect=fake_from_url),
            patch.object(client_mod, "Redis", FakeRedis),
        ):
            # Launch 20 concurrent get_redis() calls
            results = await asyncio.gather(*(client_mod.get_redis() for _ in range(20)))

        # All should return the same instance
        assert all(r is results[0] for r in results)
        # Pool should be created exactly once
        assert pool_create_count == 1
    finally:
        # Restore original state
        client_mod._pool = orig_pool
        client_mod._redis = orig_redis
        client_mod._lock = orig_lock


@pytest.mark.asyncio
async def test_get_redis_returns_cached_after_init():
    """After initialization, get_redis() should return the cached instance immediately."""
    import adaptive_lang_study_bot.cache.client as client_mod

    orig_pool, orig_redis, orig_lock = client_mod._pool, client_mod._redis, client_mod._lock

    sentinel = MagicMock()
    client_mod._redis = sentinel
    client_mod._lock = asyncio.Lock()

    try:
        result = await client_mod.get_redis()
        assert result is sentinel
    finally:
        client_mod._pool = orig_pool
        client_mod._redis = orig_redis
        client_mod._lock = orig_lock


# ---------------------------------------------------------------------------
# agent/pool.py — SessionPool concurrent acquire/release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_pool_concurrent_acquire_respects_limit():
    """Concurrent acquire calls should not exceed the semaphore limit."""
    pool = SessionPool.__new__(SessionPool)
    pool._interactive = asyncio.Semaphore(3)
    pool._proactive = asyncio.Semaphore(2)
    pool._interactive_count = 0
    pool._proactive_count = 0

    # Try to acquire 10 interactive slots concurrently (only 3 should succeed)
    results = await asyncio.gather(*(pool.acquire_interactive() for _ in range(10)))
    acquired = sum(1 for r in results if r)
    assert acquired == 3
    assert pool.interactive_active == 3


@pytest.mark.asyncio
async def test_session_pool_release_allows_reacquire():
    """Releasing a slot should allow another coroutine to acquire it."""
    pool = SessionPool.__new__(SessionPool)
    pool._interactive = asyncio.Semaphore(1)
    pool._proactive = asyncio.Semaphore(1)
    pool._interactive_count = 0
    pool._proactive_count = 0

    # Acquire the only slot
    assert await pool.acquire_interactive() is True
    assert pool.interactive_active == 1

    # Next acquire should fail
    assert await pool.acquire_interactive() is False
    assert pool.interactive_active == 1

    # Release and re-acquire
    await pool.release_interactive()
    assert pool.interactive_active == 0

    assert await pool.acquire_interactive() is True
    assert pool.interactive_active == 1

    # Cleanup
    await pool.release_interactive()


@pytest.mark.asyncio
async def test_session_pool_proactive_concurrent_acquire():
    """Proactive pool concurrent acquire respects its own limit."""
    pool = SessionPool.__new__(SessionPool)
    pool._interactive = asyncio.Semaphore(50)
    pool._proactive = asyncio.Semaphore(2)
    pool._interactive_count = 0
    pool._proactive_count = 0

    results = await asyncio.gather(*(pool.acquire_proactive() for _ in range(5)))
    acquired = sum(1 for r in results if r)
    assert acquired == 2
    assert pool.proactive_active == 2

    # Cleanup
    await pool.release_proactive()
    await pool.release_proactive()


@pytest.mark.asyncio
async def test_session_pool_counter_never_goes_negative():
    """Release on an already-zero counter should not go negative."""
    pool = SessionPool.__new__(SessionPool)
    pool._interactive = asyncio.Semaphore(5)
    pool._proactive = asyncio.Semaphore(5)
    pool._interactive_count = 0
    pool._proactive_count = 0

    # Release without prior acquire — counter should stay at 0
    await pool.release_interactive()
    assert pool.interactive_active == 0

    await pool.release_proactive()
    assert pool.proactive_active == 0
