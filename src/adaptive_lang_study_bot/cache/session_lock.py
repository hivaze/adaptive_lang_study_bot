from loguru import logger
from redis.exceptions import RedisError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import SESSION_LOCK_KEY
from adaptive_lang_study_bot.cache.redis_lock import (
    acquire_lock,
    generate_lock_token,
    refresh_lock,
    release_lock,
)


def _key(user_id: int) -> str:
    return SESSION_LOCK_KEY.format(user_id=user_id)


async def acquire_session_lock(user_id: int, ttl_seconds: int) -> str | None:
    """Try to acquire a per-user session lock. Returns an owner token if acquired, None if not.

    Fails closed on Redis errors — session creation is denied when Redis
    is temporarily unavailable (returns None).
    """
    try:
        redis = await get_redis()
        token = generate_lock_token()
        if await acquire_lock(redis, _key(user_id), token, ttl_seconds):
            return token
        return None
    except RedisError:
        logger.error("Redis unavailable during session lock acquire for user {}, denying session", user_id)
        return None


async def refresh_session_lock(user_id: int, ttl_seconds: int, token: str) -> bool:
    """Refresh TTL on an existing session lock only if we still own it.

    Returns True if refreshed, False if the lock expired or was
    acquired by someone else (indicating a potential concurrent session).
    """
    try:
        redis = await get_redis()
        return await refresh_lock(redis, _key(user_id), token, ttl_seconds)
    except RedisError:
        logger.warning("Redis unavailable during session lock refresh for user {}", user_id)
        return False


async def release_session_lock(user_id: int, token: str) -> None:
    """Release the session lock only if we still own it (conditional delete)."""
    try:
        redis = await get_redis()
        released = await release_lock(redis, _key(user_id), token)
        if not released:
            logger.debug("Session lock for user {} already expired or reacquired", user_id)
    except RedisError:
        logger.warning("Redis unavailable during session lock release for user {}", user_id)


async def has_active_session(user_id: int) -> bool:
    """Check if user has an active session."""
    try:
        redis = await get_redis()
        return await redis.exists(_key(user_id)) > 0
    except RedisError:
        logger.warning("Redis unavailable during session lock check for user {}", user_id)
        return False
