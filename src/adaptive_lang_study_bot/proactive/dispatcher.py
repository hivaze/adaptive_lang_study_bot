from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from redis.exceptions import RedisError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import (
    NOTIF_COOLDOWN_KEY,
    NOTIF_COOLDOWN_TTL,
    NOTIF_DEDUP_KEY,
    NOTIF_LLM_KEY,
    NOTIF_REMINDER_KEY,
    NOTIF_REMINDER_TTL,
)
from adaptive_lang_study_bot.config import tuning as _tuning
from adaptive_lang_study_bot.config import TIER_LIMITS
from adaptive_lang_study_bot.metrics import NOTIFICATION_LLM_COST, NOTIFICATIONS_SENT, NOTIFICATIONS_SKIPPED
from adaptive_lang_study_bot.agent.session_manager import run_proactive_llm_session
from adaptive_lang_study_bot.enums import (
    NotificationStatus,
    NotificationTier,
    ScheduleType,
    SessionType,
    UserTier,
)
from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import NotificationRepo, UserRepo
from adaptive_lang_study_bot.i18n import t
from adaptive_lang_study_bot.bot.helpers import TELEGRAM_MSG_MAX_LEN
from adaptive_lang_study_bot.proactive.triggers import Trigger
from adaptive_lang_study_bot.utils import user_local_now

# ---------------------------------------------------------------------------
# Notification type → proactive session type mappings
# ---------------------------------------------------------------------------

_SCHEDULE_TO_SESSION_TYPE: dict[str, str] = {
    ScheduleType.DAILY_REVIEW: SessionType.PROACTIVE_REVIEW,
    ScheduleType.QUIZ: SessionType.PROACTIVE_QUIZ,
    ScheduleType.PROGRESS_REPORT: SessionType.PROACTIVE_SUMMARY,
    ScheduleType.PRACTICE_REMINDER: SessionType.PROACTIVE_NUDGE,
    ScheduleType.CUSTOM: SessionType.PROACTIVE_NUDGE,
}

_TRIGGER_TO_SESSION_TYPE: dict[str, str] = {
    "streak_risk": SessionType.PROACTIVE_NUDGE,
    "cards_due": SessionType.PROACTIVE_REVIEW,
    "user_inactive": SessionType.PROACTIVE_NUDGE,
    "weak_area_persistent": SessionType.PROACTIVE_NUDGE,
    "score_trend_declining": SessionType.PROACTIVE_NUDGE,
    "score_trend_improving": SessionType.PROACTIVE_SUMMARY,
    "incomplete_exercise": SessionType.PROACTIVE_NUDGE,
    "weak_area_drill_due": SessionType.PROACTIVE_REVIEW,
    # Re-engagement triggers
    "post_onboarding_24h": SessionType.PROACTIVE_NUDGE,
    "post_onboarding_3d": SessionType.PROACTIVE_NUDGE,
    "post_onboarding_7d": SessionType.PROACTIVE_NUDGE,
    "lapsed_gentle": SessionType.PROACTIVE_NUDGE,
    "lapsed_compelling": SessionType.PROACTIVE_NUDGE,
    "lapsed_miss_you": SessionType.PROACTIVE_NUDGE,
    "post_onboarding_14d": SessionType.PROACTIVE_NUDGE,
    "dormant_weekly": SessionType.PROACTIVE_NUDGE,
    "progress_celebration": SessionType.PROACTIVE_NUDGE,
}


_TRIGGER_TO_NOTIF_CATEGORY: dict[str, str] = {
    "streak_risk": "streak_reminders",
    "cards_due": "vocab_reviews",
    "daily_review": "vocab_reviews",
    "score_trend_declining": "progress_reports",
    "score_trend_improving": "progress_reports",
    "progress_report": "progress_reports",
    "user_inactive": "re_engagement",
    "lapsed_gentle": "re_engagement",
    "lapsed_compelling": "re_engagement",
    "lapsed_miss_you": "re_engagement",
    "post_onboarding_24h": "re_engagement",
    "post_onboarding_3d": "re_engagement",
    "post_onboarding_7d": "re_engagement",
    "post_onboarding_14d": "re_engagement",
    "dormant_weekly": "re_engagement",
    "weak_area_persistent": "learning_nudges",
    "weak_area_drill_due": "learning_nudges",
    "incomplete_exercise": "learning_nudges",
    "progress_celebration": "progress_reports",
    # Schedule-driven types (so users can opt out via per-type preferences)
    "quiz": "learning_nudges",
    "practice_reminder": "streak_reminders",
    "custom": "learning_nudges",
}

# CTA button mappings: notification_type → (i18n_key, callback_data).
# Types not listed here get no CTA keyboard.
_CTA_MAPPINGS: dict[str, tuple[str, str]] = {
    "cards_due":              ("cta.start_review", "cta:words"),
    "daily_review":           ("cta.start_review", "cta:words"),
    "incomplete_exercise":    ("cta.continue", "cta:session"),
    "lapsed_gentle":          ("cta.resume_learning", "cta:session"),
    "lapsed_compelling":      ("cta.resume_learning", "cta:session"),
    "lapsed_miss_you":        ("cta.resume_learning", "cta:session"),
    "dormant_weekly":         ("cta.resume_learning", "cta:session"),
    "streak_risk":            ("cta.quick_session", "cta:session"),
    "user_inactive":          ("cta.start_session", "cta:session"),
    "weak_area_persistent":   ("cta.start_session", "cta:session"),
    "weak_area_drill_due":    ("cta.start_session", "cta:session"),
    "score_trend_declining":  ("cta.start_session", "cta:session"),
    "score_trend_improving":  ("cta.start_session", "cta:session"),
    "progress_report":        ("cta.start_session", "cta:session"),
    "post_onboarding_24h":    ("cta.start_session", "cta:session"),
    "post_onboarding_3d":     ("cta.start_session", "cta:session"),
    "post_onboarding_7d":     ("cta.start_session", "cta:session"),
    "post_onboarding_14d":    ("cta.start_session", "cta:session"),
    "quiz":                   ("cta.start_session", "cta:session"),
    "practice_reminder":      ("cta.start_session", "cta:session"),
    "custom":                 ("cta.start_session", "cta:session"),
    "progress_celebration":   ("cta.start_session", "cta:session"),
}


def build_cta_keyboard(notification_type: str, lang: str) -> InlineKeyboardMarkup | None:
    """Build a call-to-action inline keyboard differentiated by trigger type."""
    mapping = _CTA_MAPPINGS.get(notification_type)
    if mapping is None:
        return None
    i18n_key, callback_data = mapping
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(i18n_key, lang), callback_data=callback_data)],
    ])


async def _release_dedup_slot(dedup_key: str, user_id: int) -> None:
    """Release a dedup Redis slot on notification failure."""
    try:
        redis = await get_redis()
        await redis.delete(dedup_key)
    except RedisError:
        logger.warning("Redis unavailable during dedup release for user {}", user_id)


def _seconds_until_local_midnight(user: User) -> int:
    """Seconds remaining until the user's local midnight. Min 60s to avoid edge cases."""
    local_now = user_local_now(user)
    tomorrow = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(60, int((tomorrow - local_now).total_seconds()))


def _is_in_quiet_hours(user: User, local_now) -> bool:
    """Check if current local time falls within the user's quiet hours.

    Handles both same-day ranges (e.g. 08:00–22:00) and overnight
    ranges (e.g. 22:00–08:00).
    """
    if not user.quiet_hours_start or not user.quiet_hours_end:
        return False
    now_time = local_now.time()
    start, end = user.quiet_hours_start, user.quiet_hours_end
    if start <= end:
        return start <= now_time < end
    # Overnight: quiet from start until midnight, and from midnight until end
    return now_time >= start or now_time < end


async def should_send(user: User, notification_type: str) -> tuple[bool, str]:
    """Check all gates before sending a notification.

    Returns ``(can_send, skip_reason)``.  ``skip_reason`` is one of
    ``"skipped_paused"``, ``"skipped_preference"``, ``"skipped_quiet"``,
    ``"skipped_cooldown"``, ``"skipped_dedup"`` or ``""`` when sending
    is allowed.  ``"skipped_limit"`` is handled downstream in
    ``dispatch_notification()`` via atomic ``check_and_increment_notification``.

    NOTE: Dedup is checked here (read-only) as an early exit.  The
    authoritative atomic claim (SET NX) happens in dispatch_notification()
    before the Telegram send so that concurrent dispatches cannot both
    pass the gate.
    """
    if user.notifications_paused:
        return False, NotificationStatus.SKIPPED_PAUSED

    # Per-type notification preferences
    prefs = user.notification_preferences or {}
    category = _TRIGGER_TO_NOTIF_CATEGORY.get(notification_type)
    if category and not prefs.get(category, True):
        return False, NotificationStatus.SKIPPED_PREFERENCE

    # Quiet hours — use user's local time
    local_now = user_local_now(user)
    if _is_in_quiet_hours(user, local_now):
        return False, NotificationStatus.SKIPPED_QUIET

    # Daily limit check removed — the authoritative atomic
    # check_and_increment_notification() in dispatch_notification() handles
    # this correctly. Having a redundant read here doubled DB load and could
    # race with the atomic check anyway.

    # Per-user cooldown — prevents back-to-back notifications regardless of type
    try:
        redis = await get_redis()
        cooldown_key = NOTIF_COOLDOWN_KEY.format(user_id=user.telegram_id)
        if await redis.exists(cooldown_key):
            return False, NotificationStatus.SKIPPED_COOLDOWN
    except RedisError:
        logger.warning("Redis unavailable during cooldown check for user {}", user.telegram_id)

    # Dedup via Redis — read-only early exit (authoritative claim is in dispatch)
    try:
        redis = await get_redis()
        today_str = local_now.date().isoformat()
        dedup_key = NOTIF_DEDUP_KEY.format(user_id=user.telegram_id, type=notification_type, date=today_str)
        if await redis.exists(dedup_key):
            return False, NotificationStatus.SKIPPED_DEDUP
    except RedisError:
        logger.warning("Redis unavailable during notification dedup check for user {}", user.telegram_id)

    return True, ""


async def dispatch_notification(
    user: User,
    trigger: Trigger,
    bot: Bot,
) -> str | None:
    """Dispatch a notification (template or LLM). Returns the sent message text or None."""
    notification_type = trigger["type"]
    tier = trigger.get("tier", NotificationTier.TEMPLATE)
    trigger_source = trigger.get("trigger_source", "event")
    data = trigger.get("data", {})

    can_send, skip_reason = await should_send(user, notification_type)
    if not can_send:
        NOTIFICATIONS_SKIPPED.labels(reason=skip_reason).inc()
        # Only record limit-based skips in DB (useful for auditing).
        # Preference/pause/quiet/dedup skips are deterministic and would
        # generate massive table bloat if recorded every tick.
        if skip_reason == NotificationStatus.SKIPPED_LIMIT:
            async with async_session_factory() as db:
                await NotificationRepo.create(
                    db,
                    user_id=user.telegram_id,
                    notification_type=notification_type,
                    tier=tier,
                    trigger_source=trigger_source,
                    message_text="",
                    status=skip_reason,
                )
                await db.commit()
        else:
            logger.debug(
                "Notification skipped: user={} type={} reason={}",
                user.telegram_id, notification_type, skip_reason,
            )
        return None

    user_tier = UserTier(user.tier)
    now = datetime.now(timezone.utc)
    today_local = user_local_now(user).date()
    today_str = today_local.isoformat()

    # Atomic dedup claim via SET NX — done early (before the expensive LLM
    # session) to avoid wasting cost if a concurrent dispatch already claimed
    # this slot.  On send failure the slot is released so the next tick retries.
    dedup_key = NOTIF_DEDUP_KEY.format(user_id=user.telegram_id, type=notification_type, date=today_str)
    dedup_ttl = data.get("dedup_ttl") or _seconds_until_local_midnight(user)
    dedup_claimed = False
    try:
        redis = await get_redis()
        dedup_claimed = bool(await redis.set(dedup_key, "1", nx=True, ex=dedup_ttl))
        if not dedup_claimed:
            logger.debug(
                "Notification dedup hit: user={} type={} date={}",
                user.telegram_id, notification_type, today_str,
            )
            return None
    except RedisError:
        logger.warning("Redis unavailable during notification dedup pre-claim for user {}", user.telegram_id)
        # Fail open — proceed without dedup protection

    # Check LLM notification limit for free tier — atomic reservation via INCR.
    # We increment upfront so concurrent dispatches cannot both slip through.
    # If the notification ultimately fails or is downgraded, we DECR to release.
    #
    # KNOWN LIMITATION: If INCR succeeds here but the DECR at the end of this
    # function fails (Redis temporarily unavailable), the counter leaks for the
    # remainder of the day.  This can block further LLM notifications for the
    # user until midnight.  The risk is acceptable because: (1) the key has a
    # TTL that expires at local midnight, so the leak is self-healing; (2) the
    # user falls back to template notifications, not silence.
    llm_reserved = False
    llm_key = NOTIF_LLM_KEY.format(user_id=user.telegram_id, date=today_str)
    if tier == NotificationTier.LLM and user_tier == UserTier.FREE:
        limits = TIER_LIMITS[user_tier]
        try:
            redis = await get_redis()
            llm_count = await redis.incr(llm_key)
            # Set expiry on first reservation (key may have just been created)
            if llm_count == 1:
                await redis.expire(llm_key, _seconds_until_local_midnight(user))
            if llm_count > limits.max_llm_notifications_per_day:
                await redis.decr(llm_key)
                tier = NotificationTier.TEMPLATE
            else:
                llm_reserved = True
        except RedisError:
            logger.warning("Redis unavailable during LLM count check for user {}, allowing notification", user.telegram_id)
            # Fail open — allow the notification

    # Render message — LLM/hybrid or template
    message_text = None
    llm_cost = 0.0

    if tier in (NotificationTier.LLM, NotificationTier.HYBRID):
        session_type = (
            _SCHEDULE_TO_SESSION_TYPE.get(notification_type)
            or _TRIGGER_TO_SESSION_TYPE.get(notification_type)
            or SessionType.PROACTIVE_NUDGE
        )
        try:
            llm_message, llm_cost = await run_proactive_llm_session(
                user=user, session_type=session_type, trigger_data=data,
            )
            if llm_message:
                message_text = llm_message
        except Exception:
            logger.exception(
                "LLM proactive session failed for user {} (type={})",
                user.telegram_id, notification_type,
            )

        if llm_cost > 0:
            NOTIFICATION_LLM_COST.observe(llm_cost)

        if message_text is None:
            tier = NotificationTier.TEMPLATE  # Downgrade for DB recording

    if message_text is None:
        template_key = trigger.get("template_type", notification_type)
        message_text = t(f"notif.{template_key}", user.native_language, **data)

    # Telegram message limit
    if len(message_text) > TELEGRAM_MSG_MAX_LEN:
        message_text = message_text[:TELEGRAM_MSG_MAX_LEN - 3] + "..."

    # Build call-to-action keyboard based on notification type
    cta_keyboard = build_cta_keyboard(notification_type, user.native_language)

    # Atomically claim a daily notification slot BEFORE sending.
    # This prevents races where two concurrent dispatches both pass
    # the should_send() fast-path check and both deliver messages.
    #
    # We use a single DB session for both the daily-limit claim and the
    # notification record to halve connection pool pressure at scale.
    daily_limit_claimed = False
    async with async_session_factory() as db:
        daily_limit_claimed = await UserRepo.check_and_increment_notification(
            db, user.telegram_id, user.max_notifications_per_day,
            local_date=today_local,
        )
        if not daily_limit_claimed:
            status = NotificationStatus.SKIPPED_LIMIT
            await NotificationRepo.create(
                db,
                user_id=user.telegram_id,
                notification_type=notification_type,
                tier=tier,
                trigger_source=trigger.get("trigger_source", "event"),
                message_text="",
                status=status,
                cost_usd=llm_cost,
            )
            await db.commit()
            # Keep dedup slot claimed — daily limit won't reset until tomorrow,
            # so retrying every tick would just generate DB noise (skipped_limit
            # rows every 60s). The dedup key expires at local midnight anyway.
            if llm_reserved:
                try:
                    redis = await get_redis()
                    await redis.decr(llm_key)
                except RedisError:
                    pass
            return None

        # --- Daily limit claimed; send via Telegram while DB session is open ---

        try:
            sent = await bot.send_message(
                user.telegram_id,
                message_text,
                parse_mode=ParseMode.HTML,
                reply_markup=cta_keyboard,
            )
            telegram_message_id = sent.message_id
            status = NotificationStatus.SENT
        except TelegramForbiddenError:
            logger.warning("User {} blocked the bot — deactivating", user.telegram_id)
            telegram_message_id = None
            status = NotificationStatus.FAILED
            try:
                await UserRepo.update_fields(db, user.telegram_id, is_active=False)
            except Exception:
                logger.warning("Failed to deactivate blocked user {}", user.telegram_id)
        except Exception as e:
            logger.error("Failed to send notification to user {}: {}", user.telegram_id, e)
            telegram_message_id = None
            status = NotificationStatus.FAILED

        # Release dedup slot on send failure so the next tick can retry.
        if status != NotificationStatus.SENT and dedup_claimed:
            await _release_dedup_slot(dedup_key, user.telegram_id)

        # Record notification and update user state in the same session.
        await NotificationRepo.create(
            db,
            user_id=user.telegram_id,
            notification_type=notification_type,
            tier=tier,
            trigger_source=trigger.get("trigger_source", "event"),
            message_text=message_text,
            status=status,
            telegram_message_id=telegram_message_id,
            cost_usd=llm_cost,
        )

        if status == NotificationStatus.SENT:
            await UserRepo.update_fields(
                db, user.telegram_id,
                last_notification_text=message_text,
                last_notification_at=now,
            )

        await db.commit()

    # Set per-user cooldown after successful send to prevent back-to-back messages
    if status == NotificationStatus.SENT:
        try:
            redis = await get_redis()
            cooldown_key = NOTIF_COOLDOWN_KEY.format(user_id=user.telegram_id)
            await redis.set(cooldown_key, "1", ex=NOTIF_COOLDOWN_TTL)
        except RedisError:
            logger.warning("Redis unavailable during cooldown set for user {}", user.telegram_id)

    # Start follow-up reminder chain for practice_reminder notifications
    # when the user hasn't studied today (trigger data includes today_sessions)
    if (
        status == NotificationStatus.SENT
        and telegram_message_id is not None
        and notification_type == ScheduleType.PRACTICE_REMINDER
        and trigger.get("trigger_source") == "schedule"
        and trigger.get("data", {}).get("today_sessions", 0) == 0
    ):
        try:
            redis = await get_redis()
            reminder_key = NOTIF_REMINDER_KEY.format(user_id=user.telegram_id)
            now_iso = now.isoformat()
            await redis.hset(reminder_key, mapping={
                "user_id": str(user.telegram_id),
                "msg_id": str(telegram_message_id),
                "count": "1",
                "sent_at": now_iso,
                "initial_sent_at": now_iso,
                "lang": user.native_language or "en",
                "target_language": trigger.get("data", {}).get("target_language", ""),
                "user_name": user.first_name or "",
            })
            await redis.expire(reminder_key, NOTIF_REMINDER_TTL)
        except RedisError:
            logger.warning("Failed to set reminder chain for user {}", user.telegram_id)

    # Release the LLM reservation if the notification was not successfully sent as LLM
    if llm_reserved and (status != NotificationStatus.SENT or tier != NotificationTier.LLM):
        try:
            redis = await get_redis()
            await redis.decr(llm_key)
        except RedisError:
            logger.warning("Redis unavailable during LLM counter rollback for user {}", user.telegram_id)

    NOTIFICATIONS_SENT.labels(type=notification_type, tier=tier, status=status).inc()
    logger.info(
        "Notification dispatched: user={} type={} tier={} status={}",
        user.telegram_id, notification_type, tier, status,
    )

    return message_text if status == NotificationStatus.SENT else None
