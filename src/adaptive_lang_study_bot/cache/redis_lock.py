"""Safe distributed lock helpers using owner tokens.

All distributed locks should use these helpers to avoid the
unsafe-release problem where an expired lock holder deletes
a new holder's lock.
"""

import uuid

from redis.asyncio import Redis

# Lua script: delete the key only if its value matches the owner token.
# Returns 1 if deleted, 0 if the key doesn't exist or belongs to someone else.
_RELEASE_SCRIPT = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"

# Lua script: extend the TTL only if the key's value matches the owner token.
# Returns 1 if extended, 0 if the key doesn't exist or belongs to someone else.
_REFRESH_SCRIPT = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"


def generate_lock_token() -> str:
    """Generate a unique owner token for a distributed lock."""
    return uuid.uuid4().hex


async def acquire_lock(redis: Redis, key: str, token: str, ttl: int) -> bool:
    """Acquire a distributed lock with an owner token.

    Returns True if acquired, False if already held by someone else.
    """
    result = await redis.set(key, token, nx=True, ex=ttl)
    return result is not None


async def release_lock(redis: Redis, key: str, token: str) -> bool:
    """Release a distributed lock only if we still own it.

    Returns True if released, False if the lock expired or was acquired
    by someone else (safe no-op in that case).
    """
    result = await redis.eval(_RELEASE_SCRIPT, 1, key, token)
    return result == 1


async def refresh_lock(redis: Redis, key: str, token: str, ttl: int) -> bool:
    """Extend the TTL of a lock only if we still own it.

    Returns True if refreshed, False if the lock expired or was acquired
    by someone else (avoids extending another owner's lock).
    """
    result = await redis.eval(_REFRESH_SCRIPT, 1, key, token, ttl)
    return result == 1
