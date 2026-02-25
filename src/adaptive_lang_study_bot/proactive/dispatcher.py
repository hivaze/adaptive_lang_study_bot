from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from redis.exceptions import RedisError

from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import NOTIF_DEDUP_KEY, NOTIF_LLM_KEY
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
from adaptive_lang_study_bot.proactive.triggers import Trigger
from adaptive_lang_study_bot.utils import user_local_now

TELEGRAM_MSG_MAX_LEN = 4096

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
}

_DEFAULT_NOTIF_PREFS: dict[str, bool] = {
    "streak_reminders": True,
    "vocab_reviews": True,
    "progress_reports": True,
    "re_engagement": True,
    "learning_nudges": True,
}


def _build_cta_keyboard(notification_type: str, lang: str) -> InlineKeyboardMarkup | None:
    """Build a call-to-action inline keyboard differentiated by trigger type."""
    if notification_type in ("cards_due", "daily_review"):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("cta.start_review", lang), callback_data="cta:words")],
        ])
    if notification_type == "incomplete_exercise":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("cta.continue", lang), callback_data="cta:session")],
        ])
    if notification_type in (
        "lapsed_gentle", "lapsed_compelling", "lapsed_miss_you",
        "dormant_weekly",
    ):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("cta.resume_learning", lang), callback_data="cta:session")],
        ])
    if notification_type == "streak_risk":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("cta.quick_session", lang), callback_data="cta:session")],
        ])
    if notification_type in (
        "user_inactive", "weak_area_persistent", "weak_area_drill_due",
        "score_trend_declining", "score_trend_improving", "progress_report",
        "post_onboarding_24h", "post_onboarding_3d", "post_onboarding_7d",
        "post_onboarding_14d",
        "quiz", "practice_reminder", "custom",
    ):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("cta.start_session", lang), callback_data="cta:session")],
        ])
    return None


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


async def should_send(user: User, notification_type: str) -> tuple[bool, str]:
    """Check all gates before sending a notification.

    Returns ``(can_send, skip_reason)``.  ``skip_reason`` is one of
    ``"skipped_paused"``, ``"skipped_quiet"``, ``"skipped_limit"``,
    ``"skipped_dedup"`` or ``""`` when sending is allowed.

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
    if user.quiet_hours_start and user.quiet_hours_end:
        now_time = local_now.time()
        start = user.quiet_hours_start
        end = user.quiet_hours_end
        if start <= end:
            if start <= now_time < end:
                return False, NotificationStatus.SKIPPED_QUIET
        else:  # Overnight quiet hours (e.g. 22:00 - 08:00)
            if now_time >= start or now_time < end:
                return False, NotificationStatus.SKIPPED_QUIET

    # Daily limit check removed — the authoritative atomic
    # check_and_increment_notification() in dispatch_notification() handles
    # this correctly. Having a redundant read here doubled DB load and could
    # race with the atomic check anyway.

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
    cta_keyboard = _build_cta_keyboard(notification_type, user.native_language)

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
            # Release dedup slot so the next tick can retry.
            if dedup_claimed:
                await _release_dedup_slot(dedup_key, user.telegram_id)
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
