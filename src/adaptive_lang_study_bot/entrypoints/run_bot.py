import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from adaptive_lang_study_bot.bot.app import (
    create_bot,
    create_dispatcher,
    on_shutdown,
    on_startup,
)
from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.logging_config import configure_logging
from adaptive_lang_study_bot.proactive.admin_reports import (
    evaluate_health_alerts,
    send_stats_report,
)
from adaptive_lang_study_bot.proactive.tick import tick_scheduler


async def main() -> None:
    configure_logging(settings.log_level)
    logger.info("Starting LangBot...")

    bot = create_bot()
    dp = create_dispatcher()

    # Set up APScheduler for proactive engine
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        tick_scheduler,
        "interval",
        seconds=settings.proactive_tick_interval_seconds,
        args=[bot],
        id="proactive_tick",
        max_instances=1,
    )
    scheduler.add_job(
        send_stats_report,
        "interval",
        hours=settings.admin_stats_report_interval_hours,
        args=[bot],
        id="admin_stats_report",
        max_instances=1,
    )
    scheduler.add_job(
        evaluate_health_alerts,
        "interval",
        seconds=settings.proactive_tick_interval_seconds,
        args=[bot],
        id="health_alerts",
        max_instances=1,
    )

    # Register startup/shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        scheduler.start()
        logger.info("APScheduler started (interval={}s)", settings.proactive_tick_interval_seconds)

        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
