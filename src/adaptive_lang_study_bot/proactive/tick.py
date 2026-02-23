import asyncio
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import PROACTIVE_TICK_LOCK_KEY, PROACTIVE_TICK_LOCK_TTL
from adaptive_lang_study_bot.cache.redis_lock import acquire_lock, generate_lock_token, refresh_lock, release_lock
from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.metrics import PROACTIVE_TICK_DURATION, PROACTIVE_TICKS
from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.repositories import (
    ScheduleRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.enums import ScheduleStatus
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t
from adaptive_lang_study_bot.proactive.dispatcher import dispatch_notification
from adaptive_lang_study_bot.utils import compute_next_trigger, safe_zoneinfo
from adaptive_lang_study_bot.proactive.triggers import ALL_TRIGGERS

_MAX_BACKOFF_MINUTES = 1440  # 24 hours
_MAX_CONSECUTIVE_FAILURES = 10


async def _refresh_tick_lock(redis, token: str) -> None:
    """Best-effort refresh of the proactive tick lock."""
    try:
        await refresh_lock(redis, PROACTIVE_TICK_LOCK_KEY, token, PROACTIVE_TICK_LOCK_TTL)
    except RedisError:
        logger.warning("Failed to refresh proactive tick lock")


async def _periodic_lock_refresh(redis, token: str) -> None:
    """Background task that refreshes the tick lock periodically."""
    while True:
        await asyncio.sleep(tuning.proactive_lock_refresh_interval)
        await _refresh_tick_lock(redis, token)


async def tick_scheduler(bot: Bot) -> None:
    """Main proactive tick — runs every 1 minute via APScheduler.

    Two phases:
    1. Process due schedules (RRULE-based)
    2. Evaluate event triggers for active users

    Both phases use bounded parallel dispatch to keep tick duration
    short even with many LLM notifications (important at scale).
    """
    redis = await get_redis()

    # Distributed lock with owner token — only one bot instance processes the tick
    token = generate_lock_token()
    if not await acquire_lock(redis, PROACTIVE_TICK_LOCK_KEY, token, PROACTIVE_TICK_LOCK_TTL):
        return

    PROACTIVE_TICKS.inc()
    tick_start = time.monotonic()

    # Background lock refresh ensures the lock stays alive during
    # long ticks without interleaving refreshes into dispatch logic.
    refresh_task = asyncio.create_task(_periodic_lock_refresh(redis, token))

    try:
        await _phase_schedules(bot)
        await _phase_event_triggers(bot)
    except Exception:
        logger.exception("Error in proactive tick")
    finally:
        PROACTIVE_TICK_DURATION.observe(time.monotonic() - tick_start)
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await release_lock(redis, PROACTIVE_TICK_LOCK_KEY, token)


# ---------------------------------------------------------------------------
# Phase 1: Schedules
# ---------------------------------------------------------------------------

async def _phase_schedules(bot: Bot) -> None:
    """Process due schedules with bounded parallel dispatch.

    DB session is held only during the initial batch fetch, then released.
    Each dispatch and schedule update uses its own short-lived session.
    """
    # 1. Short-lived session: fetch all batch data upfront
    async with async_session_factory() as db:
        due_schedules = await ScheduleRepo.get_due(db)
        if not due_schedules:
            return

        active_user_ids = list({
            s.user_id for s in due_schedules
            if s.user and s.user.is_active
        })
        due_counts = (
            await VocabularyRepo.count_due_batch(db, active_user_ids)
            if active_user_ids else {}
        )

        schedule_ids = [s.id for s in due_schedules]
        fresh_statuses = (
            await ScheduleRepo.get_statuses_batch(db, schedule_ids)
            if schedule_ids else {}
        )

        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        session_counts = (
            await SessionRepo.count_since_batch(db, active_user_ids, week_start)
            if active_user_ids else {}
        )
    # DB session released

    # 2. Filter to actionable work items
    work_items: list[tuple] = []
    for schedule in due_schedules:
        user = schedule.user
        if user is None or not user.is_active:
            continue

        fresh_status, fresh_pause = fresh_statuses.get(
            schedule.id, (schedule.status, schedule.pause_until),
        )
        if fresh_status != ScheduleStatus.ACTIVE:
            continue
        if fresh_pause is not None and fresh_pause > datetime.now(timezone.utc):
            continue

        work_items.append((schedule, user))

    if not work_items:
        return

    # 3. Dispatch with bounded concurrency
    sem = asyncio.Semaphore(tuning.proactive_dispatch_concurrency)

    async def _process_one(schedule, user) -> None:
        async with sem:
            try:
                due_count = due_counts.get(user.telegram_id, 0)
                sessions_week = session_counts.get(user.telegram_id, 0)

                trigger = {
                    "type": schedule.schedule_type,
                    "template_type": schedule.schedule_type,
                    "tier": schedule.notification_tier,
                    "trigger_source": "schedule",
                    "data": {
                        "name": user.first_name,
                        "streak": user.streak_days,
                        "due_count": due_count,
                        "level": user.level,
                        "vocab_count": user.vocabulary_count,
                        "target_language": user.target_language,
                        "sessions_week": sessions_week,
                    },
                }

                result = await dispatch_notification(user, trigger, bot)

                # Compute next trigger time from RRULE in the user's timezone
                user_tz = safe_zoneinfo(user.timezone)
                try:
                    next_trigger = compute_next_trigger(schedule.rrule, user_tz)
                except (ValueError, TypeError):
                    logger.warning(
                        "Invalid RRULE for schedule {} (user {}): {!r}",
                        schedule.id, schedule.user_id, schedule.rrule,
                    )
                    next_trigger = None

                # Update schedule in a short-lived session
                async with async_session_factory() as db:
                    if next_trigger is None:
                        await ScheduleRepo.update_fields(
                            db, schedule.id, status=ScheduleStatus.EXPIRED,
                        )
                    else:
                        await ScheduleRepo.update_after_trigger(
                            db, schedule.id,
                            next_trigger_at=next_trigger,
                            success=result is not None,
                        )
                    await db.commit()

            except Exception:
                logger.exception(
                    "Error processing schedule {} for user {}",
                    schedule.id, schedule.user_id,
                )
                await _handle_schedule_failure(schedule, bot)

    await asyncio.gather(
        *(_process_one(s, u) for s, u in work_items),
        return_exceptions=True,
    )


async def _handle_schedule_failure(schedule, bot: Bot) -> None:
    """Increment failure counter with exponential backoff, auto-pause after 10."""
    failures = (schedule.consecutive_failures or 0) + 1
    backoff_minutes = min(2 ** failures, _MAX_BACKOFF_MINUTES)
    try:
        async with async_session_factory() as db:
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                await ScheduleRepo.update_fields(
                    db, schedule.id, status=ScheduleStatus.PAUSED,
                )
                logger.warning(
                    "Schedule {} paused after {} consecutive failures",
                    schedule.id, failures,
                )
                try:
                    user = await UserRepo.get(db, schedule.user_id)
                    lang = user.native_language if user else DEFAULT_LANGUAGE
                    await bot.send_message(
                        schedule.user_id,
                        t("settings.schedule_auto_paused", lang,
                          desc=schedule.description),
                    )
                except Exception:
                    logger.debug(
                        "Failed to notify user {} about schedule auto-pause",
                        schedule.user_id,
                    )
            else:
                await ScheduleRepo.update_after_trigger(
                    db, schedule.id,
                    next_trigger_at=datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes),
                    success=False,
                )
            await db.commit()
    except SQLAlchemyError:
        logger.exception("Failed to update schedule {} after failure", schedule.id)


# ---------------------------------------------------------------------------
# Phase 2: Event triggers
# ---------------------------------------------------------------------------

async def _phase_event_triggers(bot: Bot) -> None:
    """Evaluate event-based triggers for active users.

    Users are loaded in pages to avoid pulling 100k+ rows into memory.
    Each page is evaluated (pure Python, fast) and triggered notifications
    are dispatched with bounded concurrency.
    """
    page_size = tuning.proactive_user_page_size
    offset = 0
    sem = asyncio.Semaphore(tuning.proactive_dispatch_concurrency)

    while True:
        # Short-lived session per page
        async with async_session_factory() as db:
            users = await UserRepo.get_active_users_for_proactive(
                db, limit=page_size, offset=offset,
            )
            if not users:
                break

            user_ids = [u.telegram_id for u in users]
            due_counts = await VocabularyRepo.count_due_batch(db, user_ids)
        # DB session released

        # Evaluate triggers (pure Python, no I/O)
        work: list[tuple] = []
        for user in users:
            due_count = due_counts.get(user.telegram_id, 0)
            for trigger_fn in ALL_TRIGGERS:
                trigger = trigger_fn(user, due_count=due_count)
                if trigger is not None:
                    work.append((user, trigger))
                    break  # Only one event trigger per user per tick

        # Dispatch with bounded concurrency
        if work:
            async def _dispatch_one(u, trig) -> None:
                async with sem:
                    try:
                        await dispatch_notification(u, trig, bot)
                    except Exception:
                        logger.exception(
                            "Error dispatching trigger to user {}",
                            u.telegram_id,
                        )

            await asyncio.gather(
                *(_dispatch_one(u, trig) for u, trig in work),
                return_exceptions=True,
            )

        offset += page_size
        if len(users) < page_size:
            break  # Last page
