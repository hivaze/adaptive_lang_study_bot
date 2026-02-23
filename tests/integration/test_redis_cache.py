"""Integration tests for Redis session lock and distributed lock helpers."""

import asyncio

import pytest

from adaptive_lang_study_bot.cache.session_lock import (
    acquire_session_lock,
    has_active_session,
    refresh_session_lock,
    release_session_lock,
)

pytestmark = pytest.mark.integration


# ===========================================================================
# Session Lock (owner-token based)
# ===========================================================================

class TestSessionLock:

    async def test_acquire_returns_token(self, redis_client, mock_get_redis):
        token = await acquire_session_lock(100, ttl_seconds=60)
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0

    async def test_acquire_fails_second_time(self, redis_client, mock_get_redis):
        await acquire_session_lock(101, ttl_seconds=60)
        token2 = await acquire_session_lock(101, ttl_seconds=60)
        assert token2 is None

    async def test_release_allows_reacquire(self, redis_client, mock_get_redis):
        token = await acquire_session_lock(102, ttl_seconds=60)
        await release_session_lock(102, token)

        token2 = await acquire_session_lock(102, ttl_seconds=60)
        assert token2 is not None

    async def test_has_active_session(self, redis_client, mock_get_redis):
        assert await has_active_session(103) is False

        token = await acquire_session_lock(103, ttl_seconds=60)
        assert await has_active_session(103) is True

        await release_session_lock(103, token)
        assert await has_active_session(103) is False

    async def test_refresh_extends_ttl(self, redis_client, mock_get_redis):
        token = await acquire_session_lock(104, ttl_seconds=2)
        await refresh_session_lock(104, ttl_seconds=60, token=token)

        # After refreshing to 60s, the lock should still exist after 2.5s
        await asyncio.sleep(2.5)
        assert await has_active_session(104) is True

    async def test_ttl_auto_expiry(self, redis_client, mock_get_redis):
        await acquire_session_lock(105, ttl_seconds=1)
        assert await has_active_session(105) is True

        await asyncio.sleep(1.5)
        assert await has_active_session(105) is False

    async def test_different_users_independent(self, redis_client, mock_get_redis):
        token_a = await acquire_session_lock(200, ttl_seconds=60)
        token_b = await acquire_session_lock(201, ttl_seconds=60)

        assert token_a is not None
        assert token_b is not None

        # Both should be locked
        assert await has_active_session(200) is True
        assert await has_active_session(201) is True

        # Releasing one doesn't affect the other
        await release_session_lock(200, token_a)
        assert await has_active_session(200) is False
        assert await has_active_session(201) is True


class TestSessionLockOwnerIdentity:
    """Tests for the owner-token safety mechanism."""

    async def test_release_with_wrong_token_does_not_release(
        self, redis_client, mock_get_redis,
    ):
        token = await acquire_session_lock(300, ttl_seconds=60)
        assert token is not None

        # Try to release with a wrong token — should NOT release the lock
        await release_session_lock(300, "wrong_token")
        assert await has_active_session(300) is True

        # Release with correct token — should work
        await release_session_lock(300, token)
        assert await has_active_session(300) is False

    async def test_expired_lock_reacquired_by_new_owner(
        self, redis_client, mock_get_redis,
    ):
        """After a lock expires, a new owner can acquire it.
        The old owner's release should be a safe no-op."""
        old_token = await acquire_session_lock(301, ttl_seconds=1)
        assert old_token is not None

        await asyncio.sleep(1.5)  # Let the lock expire

        # New owner acquires
        new_token = await acquire_session_lock(301, ttl_seconds=60)
        assert new_token is not None

        # Old owner tries to release — should NOT delete new owner's lock
        await release_session_lock(301, old_token)
        assert await has_active_session(301) is True

        # New owner releases — should work
        await release_session_lock(301, new_token)
        assert await has_active_session(301) is False


class TestSessionLockRefresh:
    """Tests for refresh_session_lock behavior."""

    async def test_refresh_with_real_token_works(
        self, redis_client, mock_get_redis,
    ):
        """Normal refresh with a real token should extend TTL."""
        token = await acquire_session_lock(402, ttl_seconds=2)
        assert token is not None
        result = await refresh_session_lock(402, ttl_seconds=60, token=token)
        assert result is True
        # Lock should still exist after original TTL
        await asyncio.sleep(2.5)
        assert await has_active_session(402) is True

    async def test_refresh_with_wrong_token_returns_false(
        self, redis_client, mock_get_redis,
    ):
        """Refresh with a token that doesn't match should return False."""
        token = await acquire_session_lock(403, ttl_seconds=60)
        assert token is not None
        result = await refresh_session_lock(403, ttl_seconds=60, token="wrong_token")
        assert result is False

    async def test_refresh_expired_lock_returns_false(
        self, redis_client, mock_get_redis,
    ):
        """Refresh on an expired lock should return False."""
        token = await acquire_session_lock(404, ttl_seconds=1)
        assert token is not None
        await asyncio.sleep(1.5)  # Let it expire
        result = await refresh_session_lock(404, ttl_seconds=60, token=token)
        assert result is False
