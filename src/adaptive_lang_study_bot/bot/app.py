from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from loguru import logger

from adaptive_lang_study_bot.agent.session_manager import session_manager
from adaptive_lang_study_bot.bot.middlewares.auth import AuthMiddleware
from adaptive_lang_study_bot.bot.middlewares.rate_limit import RateLimitMiddleware
from adaptive_lang_study_bot.bot.middlewares.session import DBSessionMiddleware
from adaptive_lang_study_bot.bot.routers.chat import router as chat_router
from adaptive_lang_study_bot.bot.routers.debug import router as debug_router
from adaptive_lang_study_bot.bot.routers.review import router as review_router
from adaptive_lang_study_bot.bot.routers.settings import router as settings_router
from adaptive_lang_study_bot.bot.routers.start import router as start_router
from adaptive_lang_study_bot.bot.routers.stats import router as stats_router
from adaptive_lang_study_bot.cache.client import close_redis
from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.db.engine import dispose_engine
from adaptive_lang_study_bot.metrics import SESSION_POOL_MAX, start_metrics_server


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # Register middlewares (order matters)
    dp.message.middleware(DBSessionMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.message.middleware(RateLimitMiddleware())

    dp.callback_query.middleware(DBSessionMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(RateLimitMiddleware())

    # Register routers (order matters — specific commands before catch-all)
    dp.include_router(start_router)
    dp.include_router(review_router)
    dp.include_router(stats_router)
    dp.include_router(settings_router)
    dp.include_router(debug_router)
    dp.include_router(chat_router)  # Catch-all must be last

    return dp


async def on_startup(bot: Bot) -> None:
    logger.info("Bot starting up...")

    start_metrics_server(settings.metrics_port)
    SESSION_POOL_MAX.labels(type="interactive").set(settings.max_concurrent_interactive_sessions)
    SESSION_POOL_MAX.labels(type="proactive").set(settings.max_concurrent_proactive_sessions)

    await bot.set_my_commands([
        BotCommand(command="start", description="Start the bot / restart onboarding"),
        BotCommand(command="help", description="Show help"),
        BotCommand(command="settings", description="Open settings"),
        BotCommand(command="words", description="Review vocabulary flashcards"),
        BotCommand(command="stats", description="View your progress"),
        BotCommand(command="end", description="End current session"),
    ])
    await session_manager.start(bot)
    logger.info("Bot ready")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot shutting down...")
    await session_manager.stop()
    await close_redis()
    await dispose_engine()
    logger.info("Bot shutdown complete")
