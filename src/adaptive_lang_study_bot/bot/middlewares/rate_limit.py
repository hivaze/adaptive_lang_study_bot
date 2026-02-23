from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from loguru import logger

from redis.exceptions import RedisError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import RATE_LIMIT_KEY, RATE_LIMIT_WINDOW
from adaptive_lang_study_bot.config import TIER_LIMITS, UserTier
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t


class RateLimitMiddleware(BaseMiddleware):
    """Redis-based per-user rate limiting based on tier."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Only rate-limit text messages. Callback queries are responses to
        # bot-provided inline buttons and shouldn't count against the limit.
        if not isinstance(event, Message):
            return await handler(event, data)

        user: User | None = data.get("user")
        if user is None:
            return await handler(event, data)

        tier = UserTier(user.tier)
        limits = TIER_LIMITS[tier]
        rate_limit = limits.rate_limit_per_minute

        try:
            redis = await get_redis()
            key = RATE_LIMIT_KEY.format(user_id=user.telegram_id)

            # Use pipeline to make INCR + EXPIRE atomic, avoiding a race
            # where the key expires between INCR and the conditional EXPIRE.
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, RATE_LIMIT_WINDOW, nx=True)
                results = await pipe.execute()
            count = results[0]

            if count > rate_limit:
                lang = user.native_language or DEFAULT_LANGUAGE
                await event.answer(t("rate_limit.message", lang))
                return  # Don't call handler
        except RedisError:
            # Fail open: if Redis is unavailable, allow the event through
            # rather than silently dropping it with no response to the user.
            logger.warning("Rate limit check failed for user {} (Redis unavailable), allowing through", user.telegram_id)

        return await handler(event, data)
