"""Integration tests for distributed lock helpers against real Redis."""

import asyncio

import pytest

from adaptive_lang_study_bot.cache.redis_lock import (
    acquire_lock,
    generate_lock_token,
    refresh_lock,
    release_lock,
)

pytestmark = pytest.mark.integration


class TestAcquireLock:

    async def test_acquire_returns_true(self, redis_client, mock_get_redis):
        token = generate_lock_token()
        result = await acquire_lock(redis_client, "lock:test:1", token, ttl=60)
        assert result is True

    async def test_acquire_same_key_fails(self, redis_client, mock_get_redis):
        token1 = generate_lock_token()
        token2 = generate_lock_token()
        await acquire_lock(redis_client, "lock:test:2", token1, ttl=60)
        result = await acquire_lock(redis_client, "lock:test:2", token2, ttl=60)
        assert result is False

    async def test_acquire_different_keys_independent(self, redis_client, mock_get_redis):
        t1 = generate_lock_token()
        t2 = generate_lock_token()
        r1 = await acquire_lock(redis_client, "lock:a", t1, ttl=60)
        r2 = await acquire_lock(redis_client, "lock:b", t2, ttl=60)
        assert r1 is True
        assert r2 is True

    async def test_acquire_after_ttl_expiry(self, redis_client, mock_get_redis):
        token1 = generate_lock_token()
        await acquire_lock(redis_client, "lock:test:3", token1, ttl=1)
        await asyncio.sleep(1.5)

        token2 = generate_lock_token()
        result = await acquire_lock(redis_client, "lock:test:3", token2, ttl=60)
        assert result is True


class TestReleaseLock:

    async def test_release_with_correct_token(self, redis_client, mock_get_redis):
        token = generate_lock_token()
        await acquire_lock(redis_client, "lock:rel:1", token, ttl=60)
        result = await release_lock(redis_client, "lock:rel:1", token)
        assert result is True

        # Lock should be released — can re-acquire
        token2 = generate_lock_token()
        assert await acquire_lock(redis_client, "lock:rel:1", token2, ttl=60) is True

    async def test_release_with_wrong_token_does_not_release(self, redis_client, mock_get_redis):
        """Lua script must only delete if token matches — prevents unsafe release."""
        token = generate_lock_token()
        await acquire_lock(redis_client, "lock:rel:2", token, ttl=60)

        result = await release_lock(redis_client, "lock:rel:2", "wrong_token")
        assert result is False

        # Lock should still be held — cannot re-acquire
        token2 = generate_lock_token()
        assert await acquire_lock(redis_client, "lock:rel:2", token2, ttl=60) is False

    async def test_release_nonexistent_key(self, redis_client, mock_get_redis):
        result = await release_lock(redis_client, "lock:nonexistent", "any_token")
        assert result is False

    async def test_expired_owner_cannot_release_new_owner(self, redis_client, mock_get_redis):
        """Old owner's token must not delete new owner's lock after expiry."""
        old_token = generate_lock_token()
        await acquire_lock(redis_client, "lock:rel:3", old_token, ttl=1)
        await asyncio.sleep(1.5)

        new_token = generate_lock_token()
        await acquire_lock(redis_client, "lock:rel:3", new_token, ttl=60)

        # Old owner tries to release — must fail
        result = await release_lock(redis_client, "lock:rel:3", old_token)
        assert result is False

        # New owner's lock is still held
        assert await acquire_lock(redis_client, "lock:rel:3", generate_lock_token(), ttl=60) is False


class TestRefreshLock:

    async def test_refresh_extends_ttl(self, redis_client, mock_get_redis):
        token = generate_lock_token()
        await acquire_lock(redis_client, "lock:ref:1", token, ttl=2)

        result = await refresh_lock(redis_client, "lock:ref:1", token, ttl=60)
        assert result is True

        # After original TTL, lock should still exist
        await asyncio.sleep(2.5)
        assert await acquire_lock(redis_client, "lock:ref:1", generate_lock_token(), ttl=60) is False

    async def test_refresh_with_wrong_token_fails(self, redis_client, mock_get_redis):
        """Lua script must not extend TTL if token doesn't match."""
        token = generate_lock_token()
        await acquire_lock(redis_client, "lock:ref:2", token, ttl=60)

        result = await refresh_lock(redis_client, "lock:ref:2", "wrong", ttl=120)
        assert result is False

    async def test_refresh_expired_lock_fails(self, redis_client, mock_get_redis):
        token = generate_lock_token()
        await acquire_lock(redis_client, "lock:ref:3", token, ttl=1)
        await asyncio.sleep(1.5)

        result = await refresh_lock(redis_client, "lock:ref:3", token, ttl=60)
        assert result is False

    async def test_refresh_does_not_affect_other_owners_lock(self, redis_client, mock_get_redis):
        """If lock expired and was reacquired, old owner's refresh must not extend new lock."""
        old_token = generate_lock_token()
        await acquire_lock(redis_client, "lock:ref:4", old_token, ttl=1)
        await asyncio.sleep(1.5)

        new_token = generate_lock_token()
        await acquire_lock(redis_client, "lock:ref:4", new_token, ttl=5)

        # Old owner tries to refresh — must fail
        result = await refresh_lock(redis_client, "lock:ref:4", old_token, ttl=120)
        assert result is False

        # New owner can refresh
        result = await refresh_lock(redis_client, "lock:ref:4", new_token, ttl=120)
        assert result is True


class TestTokenUniqueness:

    def test_tokens_are_unique(self):
        tokens = {generate_lock_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_token_is_hex_string(self):
        token = generate_lock_token()
        assert isinstance(token, str)
        assert len(token) == 32  # uuid4 hex is 32 chars
        int(token, 16)  # should not raise
