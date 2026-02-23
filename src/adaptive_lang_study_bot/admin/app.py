from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import gradio as gr
from loguru import logger

from adaptive_lang_study_bot.agent.pool import session_pool
from adaptive_lang_study_bot.cache.client import get_redis
from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.repositories import (
    NotificationRepo,
    ScheduleRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.enums import UserTier
from adaptive_lang_study_bot.i18n import render_goal, render_interest
from adaptive_lang_study_bot.proactive.admin_reports import get_health_status
from sqlalchemy.exc import SQLAlchemyError

_PREVIEW_TRUNCATE_LEN = 80

# ---------------------------------------------------------------------------
# Users tab
# ---------------------------------------------------------------------------

def _format_user_rows(users: list) -> list[list]:
    """Format User objects into rows for the Gradio dataframe."""
    return [
        [
            u.telegram_id,
            u.first_name,
            f"{u.native_language}->{u.target_language}",
            u.level,
            u.streak_days,
            u.vocabulary_count,
            u.sessions_completed,
            u.tier,
            str(u.last_session_at.strftime("%Y-%m-%d %H:%M UTC") if u.last_session_at else "Never"),
            "Yes" if u.is_active else "No",
            "Yes" if u.is_admin else "No",
        ]
        for u in users
    ]


async def get_users_data() -> list[list]:
    """Get user data for the Users tab."""
    async with async_session_factory() as db:
        users = await UserRepo.list_all(db, active_only=False)
    return _format_user_rows(users)


async def search_users(query: str) -> list[list]:
    """Search users by name, username, or telegram ID."""
    if not query or not query.strip():
        return await get_users_data()
    async with async_session_factory() as db:
        users = await UserRepo.search(db, query.strip())
    return _format_user_rows(users)


async def toggle_user_tier(telegram_id: int) -> str:
    """Toggle a user between free and premium tier."""
    try:
        async with async_session_factory() as db:
            user = await UserRepo.get(db, telegram_id)
            if user is None:
                return f"User {telegram_id} not found"
            new_tier = UserTier.PREMIUM if user.tier == UserTier.FREE else UserTier.FREE
            await UserRepo.update_fields(db, telegram_id, tier=new_tier)
            await db.commit()

        logger.info("Admin: user {} tier changed to {}", telegram_id, new_tier)
        return f"User {telegram_id} ({user.first_name}) tier changed to: {new_tier}"
    except SQLAlchemyError as e:
        return f"Error: {e}"


async def toggle_user_active(telegram_id: int) -> str:
    """Toggle user active/deactivated state."""
    try:
        async with async_session_factory() as db:
            user = await UserRepo.get(db, telegram_id)
            if user is None:
                return f"User {telegram_id} not found"
            new_state = not user.is_active
            await UserRepo.update_fields(db, telegram_id, is_active=new_state)
            await db.commit()
            action = "activated" if new_state else "deactivated"
            logger.info("Admin: user {} {}", telegram_id, action)
            return f"User {telegram_id} ({user.first_name}) {action}"
    except SQLAlchemyError as e:
        return f"Error: {e}"


async def toggle_user_admin(telegram_id: int) -> str:
    """Toggle a user's admin status."""
    try:
        async with async_session_factory() as db:
            user = await UserRepo.get(db, telegram_id)
            if user is None:
                return f"User {telegram_id} not found"
            new_admin = not user.is_admin
            update_kwargs: dict[str, object] = {"is_admin": new_admin}
            # Auto-promote to premium when granting admin
            if new_admin and user.tier != UserTier.PREMIUM:
                update_kwargs["tier"] = UserTier.PREMIUM
            await UserRepo.update_fields(db, telegram_id, **update_kwargs)
            await db.commit()

        action = "granted" if new_admin else "revoked"
        logger.info("Admin: user {} admin {}", telegram_id, action)
        return f"User {telegram_id} ({user.first_name}) admin {action}"
    except SQLAlchemyError as e:
        return f"Error: {e}"


async def get_user_detail(telegram_id: int) -> str:
    """Get detailed user profile for admin inspection."""
    try:
        async with async_session_factory() as db:
            user = await UserRepo.get(db, telegram_id)
            if user is None:
                return f"User {telegram_id} not found"

            vocab_count = await VocabularyRepo.count_for_user(db, telegram_id)
            due_count = await VocabularyRepo.count_due(db, telegram_id)
            schedules = await ScheduleRepo.get_for_user(db, telegram_id, active_only=False)
            recent_sessions = await SessionRepo.get_recent(db, telegram_id, limit=5)
            recent_notifs = await NotificationRepo.get_recent(db, telegram_id, limit=5)

        lines = [
            f"=== User: {user.first_name} (ID: {user.telegram_id}) ===",
            f"Username: @{user.telegram_username or 'N/A'}",
            f"Active: {user.is_active}  |  Tier: {user.tier}  |  Admin: {user.is_admin}",
            f"Onboarding: {user.onboarding_completed}",
            f"Created: {user.created_at.strftime('%Y-%m-%d %H:%M') if user.created_at else 'N/A'}",
            "",
            "--- Learning ---",
            f"Native: {user.native_language}  |  Target: {user.target_language}",
            f"Level: {user.level}  |  Difficulty: {user.preferred_difficulty}  |  Style: {user.session_style}",
            f"Streak: {user.streak_days} days  |  Sessions: {user.sessions_completed}",
            f"Vocabulary: {vocab_count} total, {due_count} due for review",
            f"Interests: {', '.join(render_interest(i) for i in user.interests) if user.interests else 'none'}",
            f"Learning goals: {'; '.join(render_goal(g, target_language=user.target_language) for g in user.learning_goals) if user.learning_goals else 'none'}",
            f"Topics to avoid: {', '.join(user.topics_to_avoid) if user.topics_to_avoid else 'none'}",
            f"Weak areas: {', '.join(user.weak_areas) if user.weak_areas else 'none'}",
            f"Strong areas: {', '.join(user.strong_areas) if user.strong_areas else 'none'}",
            f"Recent scores: {user.recent_scores[-10:] if user.recent_scores else 'none'}",
            "",
            "--- Notifications ---",
            f"Timezone: {user.timezone}",
            f"Paused: {user.notifications_paused}",
            f"Max/day: {user.max_notifications_per_day}  |  Sent today: {user.notifications_sent_today}",
        ]
        if user.quiet_hours_start:
            lines.append(f"Quiet hours: {user.quiet_hours_start} - {user.quiet_hours_end}")
        else:
            lines.append("Quiet hours: not set")
        lines.append(
            f"Last notification: "
            f"{user.last_notification_at.strftime('%Y-%m-%d %H:%M') if user.last_notification_at else 'never'}"
        )

        if user.last_activity:
            lines.append("")
            lines.append("--- Last Activity ---")
            for k, v in user.last_activity.items():
                lines.append(f"  {k}: {v}")

        if user.session_history:
            lines.append("")
            lines.append("--- Session History ---")
            for entry in user.session_history[-5:]:
                parts = [entry.get("date", "?"), entry.get("summary", "")]
                if entry.get("topics"):
                    parts.append(f"topics: {', '.join(entry['topics'][:3])}")
                if entry.get("score") is not None:
                    parts.append(f"score: {entry['score']}/10")
                if entry.get("status") == "incomplete":
                    parts.append("(incomplete)")
                lines.append(f"  {' | '.join(parts)}")

        if schedules:
            lines.append("")
            lines.append(f"--- Schedules ({len(schedules)}) ---")
            for s in schedules:
                next_at = s.next_trigger_at.strftime("%Y-%m-%d %H:%M UTC") if s.next_trigger_at else "N/A"
                lines.append(
                    f"  [{s.status}] {s.description} "
                    f"(next: {next_at}, failures: {s.consecutive_failures})"
                )

        if recent_sessions:
            lines.append("")
            lines.append("--- Recent Sessions ---")
            for s in recent_sessions:
                cost_str = f"${float(s.cost_usd):.4f}" if s.cost_usd else "$0"
                started = s.started_at.strftime("%Y-%m-%d %H:%M UTC") if s.started_at else "?"
                lines.append(
                    f"  {started} | {s.session_type} | {s.num_turns} turns | "
                    f"{cost_str} | pipeline: {s.pipeline_status}"
                )

        if recent_notifs:
            lines.append("")
            lines.append("--- Recent Notifications ---")
            for n in recent_notifs:
                created = n.created_at.strftime("%Y-%m-%d %H:%M UTC") if n.created_at else "?"
                text_preview = (n.message_text[:_PREVIEW_TRUNCATE_LEN] + "...") if len(n.message_text) > _PREVIEW_TRUNCATE_LEN else n.message_text
                lines.append(f"  {created} | [{n.status}] {n.notification_type}: {text_preview}")

        return "\n".join(lines)

    except SQLAlchemyError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Sessions tab
# ---------------------------------------------------------------------------

async def get_sessions_data() -> list[list]:
    """Get recent sessions for the Sessions tab."""
    async with async_session_factory() as db:
        sessions = await SessionRepo.list_recent_all(db, limit=100)

    rows = []
    for s in sessions:
        rows.append([
            str(s.id)[:8],
            s.user_id,
            s.session_type,
            f"${float(s.cost_usd):.4f}" if s.cost_usd else "$0",
            s.num_turns,
            s.tool_calls_count,
            s.pipeline_status,
            str(s.started_at.strftime("%Y-%m-%d %H:%M UTC") if s.started_at else ""),
            s.duration_ms or 0,
        ])
    return rows


# ---------------------------------------------------------------------------
# Costs tab
# ---------------------------------------------------------------------------

async def get_cost_data() -> list[list]:
    """Get daily cost data for the last 14 days."""
    today = datetime.now(timezone.utc).date()
    async with async_session_factory() as db:
        daily_costs = await SessionRepo.get_daily_costs_range(db, days=14)
    rows = []
    for i in range(14):
        d = today - timedelta(days=i)
        cost = daily_costs.get(d, 0.0)
        rows.append([d.isoformat(), f"${cost:.4f}"])
    return rows


async def get_cost_per_user() -> list[list]:
    """Get per-user cost breakdown for the last 7 days."""
    async with async_session_factory() as db:
        data = await SessionRepo.get_cost_per_user(db, days=7)
    rows = []
    for user_id, first_name, total_cost, session_count in data:
        rows.append([user_id, first_name, f"${float(total_cost):.4f}", session_count])
    return rows


async def get_cost_summary() -> str:
    """Get cost summary text."""
    today = datetime.now(timezone.utc).date()
    async with async_session_factory() as db:
        cost_today = await SessionRepo.get_daily_cost(db, today)
        cost_7d = await SessionRepo.get_total_cost_range(
            db, today - timedelta(days=6), today,
        )
        cost_30d = await SessionRepo.get_total_cost_range(
            db, today - timedelta(days=29), today,
        )
        tier_counts = await UserRepo.get_tier_counts(db)
        session_types = await SessionRepo.get_session_type_counts(db, days=7)

    free_count = tier_counts.get(UserTier.FREE, 0)
    premium_count = tier_counts.get(UserTier.PREMIUM, 0)

    lines = [
        "=== Cost Summary ===",
        f"Today:     ${cost_today:.4f}",
        f"Last 7d:   ${cost_7d:.4f}  (avg ${cost_7d / 7:.4f}/day)",
        f"Last 30d:  ${cost_30d:.4f}  (avg ${cost_30d / 30:.4f}/day)",
        "",
        f"Users: {free_count} free, {premium_count} premium",
        "",
        "Session types (7d):",
    ]
    for stype, count in sorted(session_types.items()):
        lines.append(f"  {stype}: {count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alerts tab
# ---------------------------------------------------------------------------

async def get_pipeline_failures() -> list[list]:
    """Get sessions with pipeline failures."""
    async with async_session_factory() as db:
        sessions = await SessionRepo.get_pipeline_failures(db, limit=30)
    rows = []
    for s in sessions:
        issues_str = ""
        if s.pipeline_issues:
            if isinstance(s.pipeline_issues, dict):
                issues_list = s.pipeline_issues.get("issues", [])
                if isinstance(issues_list, list):
                    issues_str = "; ".join(str(i) for i in issues_list[:3])
                else:
                    issues_str = str(s.pipeline_issues)[:100]
            else:
                issues_str = str(s.pipeline_issues)[:100]
        rows.append([
            str(s.id)[:8],
            s.user_id,
            s.session_type,
            s.pipeline_status,
            issues_str,
            str(s.started_at.strftime("%Y-%m-%d %H:%M UTC") if s.started_at else ""),
        ])
    return rows


async def get_notification_stats() -> str:
    """Get notification delivery statistics."""
    async with async_session_factory() as db:
        status_counts = await NotificationRepo.get_status_counts(db, days=7)
        recent = await NotificationRepo.list_recent_all(db, limit=20)

    total = sum(status_counts.values())
    lines = [
        "=== Notification Stats (7 days) ===",
        f"Total: {total}",
    ]
    for status, count in sorted(status_counts.items()):
        pct = (count / total * 100) if total > 0 else 0
        lines.append(f"  {status}: {count} ({pct:.0f}%)")

    if recent:
        lines.append("")
        lines.append("--- Recent Notifications ---")
        for n in recent:
            created = n.created_at.strftime("%Y-%m-%d %H:%M UTC") if n.created_at else "?"
            text_preview = (n.message_text[:_PREVIEW_TRUNCATE_LEN] + "...") if len(n.message_text) > _PREVIEW_TRUNCATE_LEN else n.message_text
            lines.append(
                f"  {created} | user={n.user_id} | [{n.status}] {n.notification_type}: {text_preview}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System tab
# ---------------------------------------------------------------------------

async def get_system_health() -> str:
    """Get system health information."""
    lines = ["=== System Health ===", ""]

    # Redis
    try:
        redis = await get_redis()
        info = await redis.info("memory")
        lines.append(f"Redis memory: {info.get('used_memory_human', 'N/A')}")
        clients_info = await redis.info("clients")
        lines.append(f"Redis clients: {clients_info.get('connected_clients', 'N/A')}")
    except Exception:
        lines.append("Redis: UNAVAILABLE")

    # Database
    try:
        async with async_session_factory() as db:
            user_count = await UserRepo.count(db)
            total_users = await UserRepo.count(db, active_only=False)
        lines.append(f"Active users: {user_count} (total: {total_users})")
    except Exception:
        lines.append("Database: UNAVAILABLE")

    # Session pool
    try:
        lines.append("")
        lines.append("--- Session Pool ---")
        lines.append(
            f"Interactive: {session_pool.interactive_active}"
            f"/{settings.max_concurrent_interactive_sessions}"
        )
        lines.append(
            f"Proactive: {session_pool.proactive_active}"
            f"/{settings.max_concurrent_proactive_sessions}"
        )
    except Exception:
        lines.append("Session pool: unavailable")

    # Configuration snapshot
    lines.append("")
    lines.append("--- Configuration ---")
    lines.append(f"Proactive tick interval: {settings.proactive_tick_interval_seconds}s")
    lines.append(f"Free model: claude-haiku-4-5")
    lines.append(f"Premium model: claude-sonnet-4-6")
    lines.append(f"DB host: {settings.postgres_host}:{settings.postgres_port}")
    try:
        parsed = urlparse(settings.redis_url)
        redis_display = f"{parsed.hostname}:{parsed.port or 6379}/{parsed.path.lstrip('/') or '0'}"
    except Exception:
        redis_display = "(configured)"
    lines.append(f"Redis: {redis_display}")

    lines.append("")
    lines.append(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    return "\n".join(lines)


async def get_health_alerts_display() -> str:
    """Get health alert status for display in Gradio."""
    try:
        status = await get_health_status()
        lines = ["=== Health Alerts ===", ""]
        for check_name, check_status in status.items():
            icon = "!!" if "ALERT" in check_status else "OK" if "OK" in check_status else "??"
            lines.append(f"[{icon}] {check_name}: {check_status}")
        lines.append("")
        lines.append(f"Checked at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching health status: {e}"


# ---------------------------------------------------------------------------
# Build Gradio app
# ---------------------------------------------------------------------------

_USER_HEADERS = [
    "ID", "Name", "Languages", "Level", "Streak",
    "Vocab", "Sessions", "Tier", "Last Active", "Active", "Admin",
]


def create_admin_app() -> gr.Blocks:
    """Create the Gradio admin dashboard."""

    with gr.Blocks(title="LangBot Admin", theme=gr.themes.Soft()) as app:
        gr.Markdown("# LangBot Admin Dashboard")

        # --- Users tab ---
        with gr.Tab("Users"):
            with gr.Row():
                search_input = gr.Textbox(
                    label="Search (name, username, or Telegram ID)",
                    placeholder="Type to search...",
                    scale=3,
                )
                search_btn = gr.Button("Search", scale=1)
            users_table = gr.Dataframe(headers=_USER_HEADERS, label="Users")
            refresh_users_btn = gr.Button("Refresh All")
            search_btn.click(search_users, inputs=search_input, outputs=users_table)
            refresh_users_btn.click(get_users_data, outputs=users_table)

            gr.Markdown("### User Actions")
            action_user_id = gr.Number(label="Telegram User ID", precision=0)
            with gr.Row():
                tier_btn = gr.Button("Toggle Tier (free/premium)")
                active_btn = gr.Button("Toggle Active")
                admin_btn = gr.Button("Toggle Admin")
                detail_btn = gr.Button("View Full Profile")
            action_result = gr.Textbox(label="Result", lines=30)
            tier_btn.click(toggle_user_tier, inputs=action_user_id, outputs=action_result)
            active_btn.click(toggle_user_active, inputs=action_user_id, outputs=action_result)
            admin_btn.click(toggle_user_admin, inputs=action_user_id, outputs=action_result)
            detail_btn.click(get_user_detail, inputs=action_user_id, outputs=action_result)

        # --- Sessions tab ---
        with gr.Tab("Sessions"):
            sessions_table = gr.Dataframe(
                headers=["ID", "User", "Type", "Cost", "Turns",
                         "Tools", "Pipeline", "Started", "Duration(ms)"],
                label="Recent Sessions (last 100)",
            )
            refresh_sessions_btn = gr.Button("Refresh")
            refresh_sessions_btn.click(get_sessions_data, outputs=sessions_table)

        # --- Costs tab ---
        with gr.Tab("Costs"):
            cost_summary_text = gr.Textbox(label="Cost Summary", lines=15)
            refresh_summary_btn = gr.Button("Refresh Summary")
            refresh_summary_btn.click(get_cost_summary, outputs=cost_summary_text)

            gr.Markdown("### Daily Breakdown (14 days)")
            costs_table = gr.Dataframe(
                headers=["Date", "Total Cost"],
                label="Daily Costs",
            )
            refresh_costs_btn = gr.Button("Refresh Daily")
            refresh_costs_btn.click(get_cost_data, outputs=costs_table)

            gr.Markdown("### Per-User Costs (7 days)")
            user_costs_table = gr.Dataframe(
                headers=["User ID", "Name", "Total Cost", "Sessions"],
                label="Cost per User",
            )
            refresh_user_costs_btn = gr.Button("Refresh Per-User")
            refresh_user_costs_btn.click(get_cost_per_user, outputs=user_costs_table)

        # --- Alerts tab ---
        with gr.Tab("Alerts"):
            gr.Markdown("### Pipeline Failures")
            failures_table = gr.Dataframe(
                headers=["Session ID", "User", "Type", "Status", "Issues", "Started"],
                label="Pipeline failures (failed/pending)",
            )
            refresh_failures_btn = gr.Button("Refresh Failures")
            refresh_failures_btn.click(get_pipeline_failures, outputs=failures_table)

            gr.Markdown("### Notification Stats")
            notif_stats_text = gr.Textbox(label="Notification Statistics", lines=20)
            refresh_notif_btn = gr.Button("Refresh Notifications")
            refresh_notif_btn.click(get_notification_stats, outputs=notif_stats_text)

        # --- System tab ---
        with gr.Tab("System"):
            health_text = gr.Textbox(label="System Health", lines=25)
            refresh_health_btn = gr.Button("Refresh")
            refresh_health_btn.click(get_system_health, outputs=health_text)

            gr.Markdown("### Health Alerts")
            alerts_status_text = gr.Textbox(label="Health Alert Status", lines=15)
            refresh_alerts_btn = gr.Button("Check Health Alerts")
            refresh_alerts_btn.click(get_health_alerts_display, outputs=alerts_status_text)

    return app
