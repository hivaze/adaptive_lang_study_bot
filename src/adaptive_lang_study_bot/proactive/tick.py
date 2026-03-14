import asyncio
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import (
    NOTIF_REMINDER_TTL,
    PROACTIVE_TICK_LOCK_KEY,
    PROACTIVE_TICK_LOCK_TTL,
)
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
from adaptive_lang_study_bot.enums import ScheduleStatus, ScheduleType
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t
from adaptive_lang_study_bot.proactive.dispatcher import build_cta_keyboard, dispatch_notification
from adaptive_lang_study_bot.utils import compute_next_trigger, safe_zoneinfo
from adaptive_lang_study_bot.agent.session_manager import session_manager

# Schedule failure thresholds are in config.py:BotTuning
# (tuning.schedule_max_backoff_minutes, tuning.schedule_max_consecutive_failures)


def _local_today_start(tz_str: str) -> datetime:
    """Return the start of user's local today as a UTC datetime."""
    tz = safe_zoneinfo(tz_str)
    local_now = datetime.now(timezone.utc).astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


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

    Phase 0: Follow-up reminders for practice notifications.
    Phase 1: Process due practice_reminder schedules (RRULE-based).
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
        await _phase_reminders(bot)
        await _phase_schedules(bot)
    except Exception:
        logger.exception("Error in proactive tick")
    finally:
        PROACTIVE_TICK_DURATION.observe(time.monotonic() - tick_start)
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await release_lock(redis, PROACTIVE_TICK_LOCK_KEY, token)


async def _advance_schedule(schedule, user_timezone: str, *, success: bool) -> None:
    """Compute next trigger from RRULE and update schedule record.

    Expires the schedule if the RRULE yields no future trigger.
    """
    user_tz = safe_zoneinfo(user_timezone)
    try:
        next_trigger = compute_next_trigger(schedule.rrule, user_tz)
    except Exception:
        logger.warning(
            "Invalid RRULE for schedule {} (user {}): {!r}",
            schedule.id, schedule.user_id, schedule.rrule,
        )
        next_trigger = None

    async with async_session_factory() as db:
        if next_trigger is None:
            await ScheduleRepo.update_fields(
                db, schedule.id, status=ScheduleStatus.EXPIRED,
            )
        else:
            await ScheduleRepo.update_after_trigger(
                db, schedule.id,
                next_trigger_at=next_trigger,
                success=success,
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Phase 0: Follow-up reminders for scheduled lessons
# ---------------------------------------------------------------------------

async def _phase_reminders(bot: Bot) -> None:
    """Process pending follow-up reminders for ignored scheduled lesson notifications.

    Scans Redis for notif:reminder:{user_id} keys. For each:
    - If user started a session since the reminder was set → cancel chain.
    - If interval elapsed and count < max → send follow-up, delete previous message.
    - If count >= max → cancel chain.
    """
    redis = await get_redis()
    reminder_keys: list[str] = []
    async for key in redis.scan_iter(match="notif:reminder:*", count=100):
        reminder_keys.append(key if isinstance(key, str) else key.decode())

    if not reminder_keys:
        return

    now = datetime.now(timezone.utc)

    for key in reminder_keys:
        try:
            data = await redis.hgetall(key)
            if not data:
                await redis.delete(key)
                continue

            # Decode Redis hash values
            raw = {
                (k if isinstance(k, str) else k.decode()): (v if isinstance(v, str) else v.decode())
                for k, v in data.items()
            }
            user_id = int(raw["user_id"])
            msg_id = int(raw["msg_id"])
            count = int(raw["count"])
            sent_at = datetime.fromisoformat(raw["sent_at"])
            lang = raw.get("lang", DEFAULT_LANGUAGE)
            target_language = raw.get("target_language", "")
            user_name = raw.get("user_name", "")

            # Cancel if user started a session since reminder was set
            initial_sent_at = datetime.fromisoformat(raw["initial_sent_at"])
            async with async_session_factory() as db:
                sessions_since = await SessionRepo.count_since(
                    db, user_id, initial_sent_at,
                )
            if sessions_since > 0:
                # User started a lesson — cancel reminder chain
                await redis.delete(key)
                logger.debug("Reminder chain cancelled for user {} — session started", user_id)
                continue

            # Check if enough time elapsed
            elapsed = (now - sent_at).total_seconds()
            if elapsed < tuning.schedule_reminder_interval:
                continue

            # Check if max reminders reached
            if count >= tuning.schedule_reminder_max:
                await redis.delete(key)
                logger.debug("Reminder chain exhausted for user {} ({} reminders sent)", user_id, count)
                continue

            # Skip if user now has an active session
            if session_manager.has_active_session(user_id):
                await redis.delete(key)
                continue

            # Send follow-up reminder, delete previous message
            try:
                await bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception:
                logger.debug("Failed to delete previous reminder msg {} for user {}", msg_id, user_id)

            reminder_text = t(
                "notif.practice_reminder_followup", lang,
                name=user_name, target_language=target_language,
            )
            cta_keyboard = build_cta_keyboard(ScheduleType.PRACTICE_REMINDER, lang)
            try:
                sent = await bot.send_message(
                    user_id, reminder_text, parse_mode="HTML",
                    reply_markup=cta_keyboard,
                )
                new_msg_id = sent.message_id
            except Exception:
                logger.warning("Failed to send follow-up reminder to user {}", user_id)
                await redis.delete(key)
                continue

            # Update reminder state
            await redis.hset(key, mapping={
                "msg_id": str(new_msg_id),
                "count": str(count + 1),
                "sent_at": now.isoformat(),
            })
            await redis.expire(key, NOTIF_REMINDER_TTL)
            logger.info(
                "Sent follow-up reminder {}/{} to user {}",
                count + 1, tuning.schedule_reminder_max, user_id,
            )

        except Exception:
            logger.exception("Error processing reminder key {}", key)
            with suppress(Exception):
                await redis.delete(key)


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
                # Skip users with active interactive sessions — sending a
                # schedule notification mid-session is confusing UX.
                if session_manager.has_active_session(user.telegram_id):
                    return

                due_count = due_counts.get(user.telegram_id, 0)

                # Skip review-type schedules when no cards are due.
                if due_count == 0 and schedule.schedule_type == ScheduleType.DAILY_REVIEW:
                    await _advance_schedule(schedule, user.timezone, success=True)
                    return

                # Check if user already had a lesson today — use different template
                today_start = _local_today_start(user.timezone)
                async with async_session_factory() as db:
                    today_sessions = await SessionRepo.count_since(
                        db, user.telegram_id, today_start,
                    )

                # For practice reminders, use "another lesson" variant if already studied
                template_type = schedule.schedule_type
                if (
                    today_sessions > 0
                    and schedule.schedule_type == ScheduleType.PRACTICE_REMINDER
                ):
                    template_type = "practice_reminder_another"

                target_lang_name = t(f"lang.{user.target_language}", user.native_language)

                streak_info = ""
                if user.streak_days > 1:
                    streak_info = t(
                        "notif.streak_info", user.native_language,
                        streak=user.streak_days,
                    )

                trigger = {
                    "type": schedule.schedule_type,
                    "template_type": template_type,
                    "tier": schedule.notification_tier,
                    "trigger_source": "schedule",
                    "data": {
                        "name": user.first_name,
                        "streak": user.streak_days,
                        "streak_info": streak_info,
                        "due_count": due_count,
                        "level": user.level,
                        "vocab_count": user.vocabulary_count,
                        "target_language": target_lang_name,
                        "sessions_week": session_counts.get(user.telegram_id, 0),
                        "today_sessions": today_sessions,
                    },
                }

                result = await dispatch_notification(user, trigger, bot)
                await _advance_schedule(schedule, user.timezone, success=result is not None)

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
    backoff_minutes = min(2 ** failures, tuning.schedule_max_backoff_minutes)
    try:
        async with async_session_factory() as db:
            if failures >= tuning.schedule_max_consecutive_failures:
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

