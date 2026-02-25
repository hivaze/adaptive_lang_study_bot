from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from loguru import logger

from adaptive_lang_study_bot.agent.pool import session_pool
from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.cache.keys import (
    ADMIN_ALERT_DEDUP_KEY,
    ADMIN_ALERT_DEDUP_TTL,
    ADMIN_HEALTH_LOCK_KEY,
    ADMIN_HEALTH_LOCK_TTL,
    ADMIN_STATS_LOCK_KEY,
    ADMIN_STATS_LOCK_TTL,
)
from adaptive_lang_study_bot.cache.redis_lock import acquire_lock, generate_lock_token, release_lock
from adaptive_lang_study_bot.config import settings, tuning
from adaptive_lang_study_bot.enums import NotificationStatus, UserTier
from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.repositories import (
    NotificationRepo,
    SessionRepo,
    UserRepo,
)


# Health check thresholds are in config.py:BotTuning
# (tuning.pool_usage_alert_pct, tuning.pipeline_failure_threshold, etc.)

# ---------------------------------------------------------------------------
# Stats Report (runs every 12 hours)
# ---------------------------------------------------------------------------

async def build_stats_report() -> str:
    """Build the periodic admin stats report text."""
    today = datetime.now(timezone.utc).date()
    async with async_session_factory() as db:
        active_users = await UserRepo.count(db, active_only=True)
        total_users = await UserRepo.count(db, active_only=False)
        tier_counts = await UserRepo.get_tier_counts(db)
        sessions_today = await SessionRepo.count_today_all(db)
        cost_today = await SessionRepo.get_daily_cost(db, today)
        cost_7d = await SessionRepo.get_total_cost_range(
            db, today - timedelta(days=6), today,
        )
        cost_30d = await SessionRepo.get_total_cost_range(
            db, today - timedelta(days=29), today,
        )
        notif_stats = await NotificationRepo.get_status_counts(db, days=1)
        session_types = await SessionRepo.get_session_type_counts(db, days=1)

    free_count = tier_counts.get(UserTier.FREE, 0)
    premium_count = tier_counts.get(UserTier.PREMIUM, 0)
    notif_total = sum(notif_stats.values())
    notif_sent = notif_stats.get(NotificationStatus.SENT, 0)
    notif_failed = notif_stats.get(NotificationStatus.FAILED, 0)

    lines = [
        "=== Admin Stats Report ===",
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Active users: {active_users} (total: {total_users})",
        f"Tiers: {free_count} free, {premium_count} premium",
        f"Sessions today: {sessions_today}",
        "",
        "--- Costs ---",
        f"Today: ${cost_today:.4f}",
        f"7-day avg: ${cost_7d / 7:.4f}/day",
        f"30-day avg: ${cost_30d / 30:.4f}/day",
        "",
        "--- Session Pool ---",
        f"Interactive: {session_pool.interactive_active}/{settings.max_concurrent_interactive_sessions}",
        f"Proactive: {session_pool.proactive_active}/{settings.max_concurrent_proactive_sessions}",
        "",
        "--- Notifications (today) ---",
        f"Total: {notif_total} (sent: {notif_sent}, failed: {notif_failed})",
    ]

    if session_types:
        lines.append("")
        lines.append("--- Session Types (today) ---")
        for stype, count in sorted(session_types.items()):
            lines.append(f"  {stype}: {count}")

    return "\n".join(lines)


async def send_stats_report(bot: Bot) -> None:
    """Build and send the stats report to all admins."""
    redis = await get_redis()
    token = generate_lock_token()
    if not await acquire_lock(redis, ADMIN_STATS_LOCK_KEY, token, ADMIN_STATS_LOCK_TTL):
        return

    try:
        report = await build_stats_report()
        await _send_to_all_admins(bot, report)
        logger.info("Admin stats report sent")
    except Exception:
        logger.exception("Failed to send admin stats report")
    finally:
        await release_lock(redis, ADMIN_STATS_LOCK_KEY, token)


# ---------------------------------------------------------------------------
# Shared health check infrastructure
# ---------------------------------------------------------------------------

@dataclass
class HealthCheckResult:
    """Result of a single health check, used by both alerts and Gradio status."""

    check_name: str
    is_alert: bool
    alert_message: str
    status_message: str


async def _check_cost_spike() -> HealthCheckResult:
    today = datetime.now(timezone.utc).date()
    async with async_session_factory() as db:
        cost_today = await SessionRepo.get_daily_cost(db, today)
        avg_7d = await SessionRepo.get_daily_cost_average(db, days=7)
    is_alert = avg_7d > 0 and cost_today > 2 * avg_7d
    return HealthCheckResult(
        check_name="cost_spike",
        is_alert=is_alert,
        alert_message=f"COST SPIKE: Today ${cost_today:.4f} > 2x 7-day avg ${avg_7d:.4f}/day",
        status_message=(
            f"ALERT: ${cost_today:.4f} > 2x avg ${avg_7d:.4f}"
            if is_alert
            else f"OK (today: ${cost_today:.4f}, avg: ${avg_7d:.4f})"
        ),
    )


async def _check_pool_interactive() -> HealthCheckResult:
    interactive_pct = (
        session_pool.interactive_active
        / max(1, settings.max_concurrent_interactive_sessions) * 100
    )
    is_alert = interactive_pct >= tuning.pool_usage_alert_pct
    return HealthCheckResult(
        check_name="high_pool_interactive",
        is_alert=is_alert,
        alert_message=(
            f"HIGH POOL: Interactive at {interactive_pct:.0f}% "
            f"({session_pool.interactive_active}/{settings.max_concurrent_interactive_sessions})"
        ),
        status_message=f"Interactive: {interactive_pct:.0f}%",
    )


async def _check_pool_proactive() -> HealthCheckResult:
    proactive_pct = (
        session_pool.proactive_active
        / max(1, settings.max_concurrent_proactive_sessions) * 100
    )
    is_alert = proactive_pct >= tuning.pool_usage_alert_pct
    return HealthCheckResult(
        check_name="high_pool_proactive",
        is_alert=is_alert,
        alert_message=(
            f"HIGH POOL: Proactive at {proactive_pct:.0f}% "
            f"({session_pool.proactive_active}/{settings.max_concurrent_proactive_sessions})"
        ),
        status_message=f"Proactive: {proactive_pct:.0f}%",
    )


async def _check_pipeline_failures() -> HealthCheckResult:
    async with async_session_factory() as db:
        failures = await SessionRepo.count_pipeline_failures_recent(db, hours=1)
    is_alert = failures > tuning.pipeline_failure_threshold
    return HealthCheckResult(
        check_name="pipeline_failures",
        is_alert=is_alert,
        alert_message=f"PIPELINE FAILURES: {failures} failures in the last hour",
        status_message=(
            f"ALERT: {failures} failures in last hour"
            if is_alert
            else f"OK ({failures} failures in last hour)"
        ),
    )


async def _check_redis() -> HealthCheckResult:
    try:
        redis = await get_redis()
        await redis.ping()
        return HealthCheckResult(
            check_name="redis",
            is_alert=False,
            alert_message="REDIS UNHEALTHY: Connection failed",
            status_message="OK",
        )
    except Exception:
        return HealthCheckResult(
            check_name="redis",
            is_alert=True,
            alert_message="REDIS UNHEALTHY: Connection failed",
            status_message="ALERT: Connection failed",
        )


async def _check_db() -> HealthCheckResult:
    try:
        async with async_session_factory() as db:
            await UserRepo.count(db)
        return HealthCheckResult(
            check_name="database",
            is_alert=False,
            alert_message="DB UNHEALTHY: Connection failed",
            status_message="OK",
        )
    except Exception:
        return HealthCheckResult(
            check_name="database",
            is_alert=True,
            alert_message="DB UNHEALTHY: Connection failed",
            status_message="ALERT: Connection failed",
        )


async def _check_notification_failures() -> HealthCheckResult:
    async with async_session_factory() as db:
        failed, total = await NotificationRepo.get_failure_rate_recent(db, hours=1)
    is_alert = total >= tuning.notif_failure_min_total and (failed / total) > tuning.notif_failure_rate_threshold
    if is_alert:
        rate = failed / total * 100
        alert_msg = f"NOTIFICATION FAILURES: {rate:.0f}% failure rate ({failed}/{total}) in last hour"
        status_msg = f"ALERT: {failed}/{total} failed ({rate:.0f}%)"
    else:
        alert_msg = ""
        status_msg = f"OK ({failed}/{total} failed)"
    return HealthCheckResult(
        check_name="notification_failures",
        is_alert=is_alert,
        alert_message=alert_msg,
        status_message=status_msg,
    )


# Redis and DB checks handle their own exceptions internally (connectivity is
# the thing being checked), so they are separated from the rest.
_HEALTH_CHECKS_WITH_INTERNAL_ERROR_HANDLING = [_check_redis, _check_db]
_HEALTH_CHECKS_STANDARD = [
    _check_cost_spike,
    _check_pool_interactive,
    _check_pool_proactive,
    _check_pipeline_failures,
    _check_notification_failures,
]


# ---------------------------------------------------------------------------
# Health Alerts (runs every ~60s)
# ---------------------------------------------------------------------------

async def evaluate_health_alerts(bot: Bot) -> None:
    """Evaluate all health alert conditions and send alerts as needed."""
    redis = await get_redis()
    token = generate_lock_token()
    if not await acquire_lock(redis, ADMIN_HEALTH_LOCK_KEY, token, ADMIN_HEALTH_LOCK_TTL):
        return

    try:
        all_checks = _HEALTH_CHECKS_STANDARD + _HEALTH_CHECKS_WITH_INTERNAL_ERROR_HANDLING
        for check_fn in all_checks:
            try:
                result = await check_fn()
                if result.is_alert:
                    await _send_alert_if_not_deduped(bot, result.check_name, result.alert_message)
            except Exception:
                logger.warning("Health check failed: {}", check_fn.__name__)
                await _send_alert_if_not_deduped(
                    bot, f"{check_fn.__name__}_error",
                    f"Health check {check_fn.__name__} raised an exception",
                )

    except Exception:
        logger.exception("Error in health alert evaluation")
    finally:
        await release_lock(redis, ADMIN_HEALTH_LOCK_KEY, token)


# ---------------------------------------------------------------------------
# Health status for Gradio
# ---------------------------------------------------------------------------

async def get_health_status() -> dict[str, str]:
    """Evaluate health checks and return alert_type -> status string.

    Used by the Gradio admin panel System tab.
    """
    results: dict[str, str] = {}

    for check_fn in _HEALTH_CHECKS_STANDARD:
        try:
            result = await check_fn()
            results[result.check_name] = result.status_message
        except Exception as e:
            results[check_fn.__name__.removeprefix("_check_")] = f"ERROR: {e}"

    for check_fn in _HEALTH_CHECKS_WITH_INTERNAL_ERROR_HANDLING:
        result = await check_fn()
        results[result.check_name] = result.status_message

    # Combine pool checks into a single "pool_usage" entry for Gradio display
    interactive_status = results.pop("high_pool_interactive", "")
    proactive_status = results.pop("high_pool_proactive", "")
    combined = f"{interactive_status}, {proactive_status}"
    if "ALERT" in interactive_status or "ALERT" in proactive_status:
        results["pool_usage"] = f"ALERT: {combined}"
    else:
        results["pool_usage"] = f"OK ({combined})"

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send_alert_if_not_deduped(bot: Bot, alert_type: str, message: str) -> None:
    """Send an alert to all admins if not deduped (1-hour cooldown per type)."""
    redis = await get_redis()
    now = datetime.now(timezone.utc)
    date_hour = now.strftime("%Y-%m-%d_%H")
    dedup_key = ADMIN_ALERT_DEDUP_KEY.format(alert_type=alert_type, date_hour=date_hour)

    was_set = await redis.set(dedup_key, "1", nx=True, ex=ADMIN_ALERT_DEDUP_TTL)
    if not was_set:
        logger.debug("Alert {} deduped", alert_type)
        return

    full_message = f"[HEALTH ALERT] {message}"
    await _send_to_all_admins(bot, full_message)
    logger.warning("Health alert sent: {}", alert_type)


async def _send_to_all_admins(bot: Bot, text: str) -> None:
    """Send a plain-text message to all admin users via Telegram."""
    async with async_session_factory() as db:
        admin_ids = await UserRepo.get_all_admin_ids(db)

    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:
            logger.warning("Failed to send admin message to {}", admin_id)
