import asyncio

from redis.asyncio import ConnectionPool, Redis

from adaptive_lang_study_bot.config import settings

_pool: ConnectionPool | None = None
_redis: Redis | None = None
_lock: asyncio.Lock = asyncio.Lock()


async def get_redis() -> Redis:
    """Get or create the global Redis connection."""
    global _pool, _redis
    if _redis is not None:
        return _redis
    async with _lock:
        if _redis is None:
            _pool = ConnectionPool.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=settings.redis_max_connections,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
            )
            _redis = Redis(connection_pool=_pool)
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _pool, _redis
    async with _lock:
        if _redis is not None:
            await _redis.aclose()
            _redis = None
        if _pool is not None:
            await _pool.aclose()
            _pool = None
