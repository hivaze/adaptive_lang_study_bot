import asyncio
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
# Custom CSS
# ---------------------------------------------------------------------------

ADMIN_THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.neutral,
    secondary_hue=gr.themes.colors.neutral,
    neutral_hue=gr.themes.colors.gray,
    font=gr.themes.GoogleFont("Inter"),
)

ADMIN_CSS = """
/* ─────────────────────────────────────────────────────────
   Dark foundation — override Gradio CSS variables
   ───────────────────────────────────────────────────────── */
:root,
.gradio-container,
.dark {
    /* Surfaces */
    --body-background-fill: #09090b !important;
    --background-fill-primary: #0f0f11 !important;
    --background-fill-secondary: #141416 !important;
    --block-background-fill: #0f0f11 !important;
    --block-border-color: #1e1e22 !important;
    --block-label-background-fill: #141416 !important;
    --panel-background-fill: #0c0c0e !important;
    --color-accent-soft: #18181b !important;
    /* Typography */
    --block-label-text-color: #71717a !important;
    --block-title-text-color: #d4d4d8 !important;
    --body-text-color: #a1a1aa !important;
    --body-text-color-subdued: #52525b !important;
    /* Borders */
    --border-color-primary: #1e1e22 !important;
    --border-color-accent: #27272a !important;
    /* Inputs */
    --input-background-fill: #111113 !important;
    --input-border-color: #27272a !important;
    --input-placeholder-color: #3f3f46 !important;
    /* Buttons */
    --button-primary-background-fill: #fafafa !important;
    --button-primary-text-color: #09090b !important;
    --button-primary-background-fill-hover: #d4d4d8 !important;
    --button-secondary-background-fill: #18181b !important;
    --button-secondary-text-color: #a1a1aa !important;
    --button-secondary-border-color: #27272a !important;
    --button-secondary-background-fill-hover: #1e1e22 !important;
    /* Tables */
    --table-border-color: #1e1e22 !important;
    --table-even-background-fill: #0f0f11 !important;
    --table-odd-background-fill: #111113 !important;
    --table-row-focus: #18181b !important;
    /* Misc */
    --checkbox-background-color: #18181b !important;
    --checkbox-border-color: #27272a !important;
    --shadow-drop: none !important;
    --shadow-drop-lg: none !important;
    --accordion-text-color: #a1a1aa !important;
    color-scheme: dark;
}
body, .gradio-container {
    background: #09090b !important;
    color: #a1a1aa !important;
}

/* ─────────────────────────────────────────────────────────
   Header
   ───────────────────────────────────────────────────────── */
.admin-header {
    background: #0f0f11;
    border: 1px solid #1e1e22;
    border-bottom: 1px solid #27272a;
    padding: 1.5rem 1.75rem;
    border-radius: 12px;
    margin-bottom: 0.75rem;
}
.admin-header h1 {
    margin: 0;
    font-size: 1.35rem;
    font-weight: 500;
    color: #fafafa !important;
    letter-spacing: -0.025em;
}
.admin-header p {
    margin: 0.35rem 0 0;
    font-size: 0.82rem;
    color: #52525b !important;
    letter-spacing: 0.01em;
}

/* ─────────────────────────────────────────────────────────
   Badges — monochrome with subtle hierarchy
   ───────────────────────────────────────────────────────── */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 10px;
    font-size: 0.75rem;
    font-weight: 500;
    line-height: 1.7;
    white-space: nowrap;
    letter-spacing: 0.02em;
    transition: opacity 0.15s ease;
}
.badge-light {
    background: #fafafa;
    color: #09090b;
}
.badge-dark {
    background: #18181b;
    color: #71717a;
    border: 1px solid #27272a;
}
.badge-mid {
    background: #27272a;
    color: #d4d4d8;
}
.badge-accent {
    background: #18181b;
    color: #e4e4e7;
    border: 1px solid #3f3f46;
}
.badge-alert {
    background: #fafafa;
    color: #09090b;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.7rem;
}

/* ─────────────────────────────────────────────────────────
   Stat cards
   ───────────────────────────────────────────────────────── */
.stat-row {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin: 0.75rem 0;
}
.stat-card {
    flex: 1;
    min-width: 140px;
    background: #0f0f11;
    border: 1px solid #1e1e22;
    border-radius: 10px;
    padding: 1rem 1.25rem;
    text-align: center;
    transition: border-color 0.2s ease;
}
.stat-card:hover {
    border-color: #3f3f46;
}
.stat-card .stat-value {
    font-size: 1.6rem;
    font-weight: 600;
    color: #fafafa;
    letter-spacing: -0.03em;
}
.stat-card .stat-label {
    font-size: 0.72rem;
    font-weight: 400;
    color: #52525b;
    margin-top: 0.25rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

/* ─────────────────────────────────────────────────────────
   Health indicators
   ───────────────────────────────────────────────────────── */
.health-ok {
    color: #d4d4d8;
    font-weight: 500;
}
.health-alert {
    color: #fafafa;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    background: #27272a;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
}
.health-unknown {
    color: #52525b;
    font-weight: 500;
}

/* ─────────────────────────────────────────────────────────
   Report Markdown — tables & rules
   ───────────────────────────────────────────────────────── */
.report-md table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.5rem 0;
}
.report-md th,
.report-md td {
    padding: 8px 14px;
    border-bottom: 1px solid #1e1e22;
    text-align: left;
    font-size: 0.85rem;
    color: #a1a1aa;
}
.report-md th {
    font-weight: 500;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #52525b;
    background: transparent;
    border-bottom: 1px solid #27272a;
}
.report-md hr {
    border: none;
    border-top: 1px solid #1e1e22;
    margin: 1rem 0;
}
.report-md h3 {
    color: #d4d4d8 !important;
    font-weight: 500;
    letter-spacing: -0.01em;
}
.report-md strong {
    color: #d4d4d8;
    font-weight: 500;
}
.report-md em {
    color: #52525b;
    font-style: normal;
    font-size: 0.8rem;
}
"""

# ---------------------------------------------------------------------------
# Badge helpers
# ---------------------------------------------------------------------------


def _tier_badge(tier: str) -> str:
    if tier == UserTier.PREMIUM:
        return '<span class="badge badge-light">PREMIUM</span>'
    return '<span class="badge badge-dark">FREE</span>'


def _active_badge(is_active: bool) -> str:
    if is_active:
        return '<span class="badge badge-light">Active</span>'
    return '<span class="badge badge-alert">Inactive</span>'


def _status_badge(status: str) -> str:
    styles = {
        "completed": "light", "sent": "light",
        "failed": "alert",
        "pending": "mid", "running": "accent",
        "active": "light", "paused": "mid", "expired": "dark",
        "skipped_quiet": "mid", "skipped_paused": "mid",
        "skipped_preference": "mid", "skipped_limit": "mid",
        "skipped_dedup": "dark",
    }
    style = styles.get(status, "dark")
    return f'<span class="badge badge-{style}">{status}</span>'


def _health_icon(status_str: str) -> str:
    if "ALERT" in status_str:
        return '<span class="health-alert">ALERT</span>'
    if "OK" in status_str:
        return '<span class="health-ok">OK</span>'
    return '<span class="health-unknown">??</span>'


def _esc(text: str) -> str:
    """Escape pipe characters for Markdown table cells."""
    return text.replace("|", "\\|")


# ---------------------------------------------------------------------------
# Users tab
# ---------------------------------------------------------------------------

_USER_HEADERS = [
    "ID", "Name", "Languages", "Level", "Streak",
    "Vocab", "Sessions", "Tier", "Last Active", "Active", "Admin",
]


def _format_user_rows(users: list) -> list[list]:
    """Format User objects into rows for the Gradio dataframe."""
    return [
        [
            u.telegram_id,
            u.first_name,
            f"{u.native_language} -> {u.target_language}",
            u.level,
            f"{u.streak_days}d",
            u.vocabulary_count,
            u.sessions_completed,
            str(u.tier).upper(),
            u.last_session_at.strftime("%m/%d %H:%M") if u.last_session_at else "Never",
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
        return f"**Done:** User {telegram_id} ({user.first_name}) tier changed to {_tier_badge(new_tier)}"
    except SQLAlchemyError as e:
        return f"**Error:** {e}"


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
            return f"**Done:** User {telegram_id} ({user.first_name}) {action} {_active_badge(new_state)}"
    except SQLAlchemyError as e:
        return f"**Error:** {e}"


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
        badge = '<span class="badge badge-accent">Admin</span>' if new_admin else '<span class="badge badge-dark">User</span>'
        logger.info("Admin: user {} admin {}", telegram_id, action)
        return f"**Done:** User {telegram_id} ({user.first_name}) admin {action} {badge}"
    except SQLAlchemyError as e:
        return f"**Error:** {e}"


async def get_user_detail(telegram_id: int) -> str:
    """Get detailed user profile for admin inspection (Markdown)."""
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

        onboarding_badge = (
            '<span class="badge badge-light">Complete</span>'
            if user.onboarding_completed
            else '<span class="badge badge-mid">Pending</span>'
        )
        admin_badge = ' <span class="badge badge-accent">Admin</span>' if user.is_admin else ""
        created = user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "N/A"

        lines = [
            f"### {_esc(user.first_name)} (ID: {user.telegram_id})",
            f"**Username:** @{user.telegram_username or 'N/A'} &nbsp; "
            f"{_active_badge(user.is_active)} {_tier_badge(user.tier)}{admin_badge}",
            f"**Onboarding:** {onboarding_badge} &nbsp; **Created:** {created}",
            "",
            "---",
            "",
            "### Learning",
            "",
            f"| | |",
            f"|---|---|",
            f"| **Languages** | {user.native_language} -> {user.target_language} |",
            f"| **Level** | {user.level} |",
            f"| **Difficulty** | {user.preferred_difficulty} |",
            f"| **Style** | {user.session_style} |",
            f"| **Streak** | {user.streak_days} days |",
            f"| **Sessions** | {user.sessions_completed} |",
            f"| **Vocabulary** | {vocab_count} total, {due_count} due |",
            "",
            f"**Interests:** {', '.join(render_interest(i) for i in user.interests) if user.interests else 'none'}",
            f"**Learning goals:** {'; '.join(render_goal(g, target_language=user.target_language) for g in user.learning_goals) if user.learning_goals else 'none'}",
            f"**Topics to avoid:** {', '.join(user.topics_to_avoid) if user.topics_to_avoid else 'none'}",
            f"**Weak areas:** {', '.join(user.weak_areas) if user.weak_areas else 'none'}",
            f"**Strong areas:** {', '.join(user.strong_areas) if user.strong_areas else 'none'}",
            f"**Recent scores:** {user.recent_scores[-10:] if user.recent_scores else 'none'}",
            "",
            "---",
            "",
            "### Notifications",
            "",
        ]
        quiet = (
            f"{user.quiet_hours_start} - {user.quiet_hours_end}"
            if user.quiet_hours_start
            else "not set"
        )
        last_notif = (
            user.last_notification_at.strftime("%Y-%m-%d %H:%M")
            if user.last_notification_at
            else "never"
        )
        paused_badge = (
            '<span class="badge badge-mid">Paused</span>'
            if user.notifications_paused
            else '<span class="badge badge-light">Active</span>'
        )
        lines += [
            f"| | |",
            f"|---|---|",
            f"| **Timezone** | {user.timezone} |",
            f"| **Status** | {paused_badge} |",
            f"| **Max / day** | {user.max_notifications_per_day} |",
            f"| **Sent today** | {user.notifications_sent_today} |",
            f"| **Quiet hours** | {quiet} |",
            f"| **Last sent** | {last_notif} |",
        ]

        if user.last_activity:
            lines += ["", "---", "", "### Last Activity", ""]
            for k, v in user.last_activity.items():
                lines.append(f"- **{k}:** {v}")

        if user.session_history:
            lines += ["", "---", "", "### Session History", ""]
            lines.append("| Date | Summary | Topics | Score | Status |")
            lines.append("|------|---------|--------|-------|--------|")
            for entry in user.session_history[-5:]:
                date = entry.get("date", "?")
                summary = _esc(entry.get("summary", ""))
                topics = _esc(", ".join(entry.get("topics", [])[:3])) or "-"
                score = f"{entry['score']}/10" if entry.get("score") is not None else "-"
                status = entry.get("status", "complete")
                lines.append(f"| {date} | {summary} | {topics} | {score} | {status} |")

        if schedules:
            lines += ["", "---", "", f"### Schedules ({len(schedules)})", ""]
            lines.append("| Status | Description | Next trigger | Failures |")
            lines.append("|--------|-------------|--------------|----------|")
            for s in schedules:
                next_at = s.next_trigger_at.strftime("%Y-%m-%d %H:%M UTC") if s.next_trigger_at else "N/A"
                lines.append(
                    f"| {_status_badge(s.status)} | {_esc(s.description)} "
                    f"| {next_at} | {s.consecutive_failures} |"
                )

        if recent_sessions:
            lines += ["", "---", "", "### Recent Sessions", ""]
            lines.append("| Started | Type | Turns | Cost | Pipeline |")
            lines.append("|---------|------|-------|------|----------|")
            for s in recent_sessions:
                cost_str = f"${float(s.cost_usd):.4f}" if s.cost_usd else "$0"
                started = s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else "?"
                lines.append(
                    f"| {started} | {s.session_type} | {s.num_turns} "
                    f"| {cost_str} | {_status_badge(s.pipeline_status)} |"
                )

        if recent_notifs:
            lines += ["", "---", "", "### Recent Notifications", ""]
            lines.append("| Time | Status | Type | Preview |")
            lines.append("|------|--------|------|---------|")
            for n in recent_notifs:
                created = n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "?"
                preview = _esc(
                    (n.message_text[:_PREVIEW_TRUNCATE_LEN] + "...")
                    if len(n.message_text) > _PREVIEW_TRUNCATE_LEN
                    else n.message_text
                )
                lines.append(
                    f"| {created} | {_status_badge(n.status)} "
                    f"| {n.notification_type} | {preview} |"
                )

        return "\n".join(lines)

    except SQLAlchemyError as e:
        return f"**Error:** {e}"


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
    """Get cost summary as Markdown with stat cards."""
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

    type_rows = "\n".join(
        f"| {stype} | {count} |" for stype, count in sorted(session_types.items())
    )

    return f"""\
<div class="stat-row">
<div class="stat-card"><div class="stat-value">${cost_today:.4f}</div><div class="stat-label">Today</div></div>
<div class="stat-card"><div class="stat-value">${cost_7d:.4f}</div><div class="stat-label">Last 7 days</div></div>
<div class="stat-card"><div class="stat-value">${cost_30d:.4f}</div><div class="stat-label">Last 30 days</div></div>
<div class="stat-card"><div class="stat-value">${cost_7d / 7:.4f}</div><div class="stat-label">Avg / day (7d)</div></div>
</div>

**Users:** {free_count} free, {premium_count} premium

**Session types (7d):**

| Type | Count |
|------|-------|
{type_rows}
"""


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
    """Get notification delivery statistics as Markdown."""
    async with async_session_factory() as db:
        status_counts = await NotificationRepo.get_status_counts(db, days=7)
        recent = await NotificationRepo.list_recent_all(db, limit=20)

    total = sum(status_counts.values())

    status_rows = "\n".join(
        f"| {_status_badge(status)} | {count} | {(count / total * 100) if total > 0 else 0:.0f}% |"
        for status, count in sorted(status_counts.items())
    )

    lines = [
        f"### Stats (7 days) &mdash; Total: **{total}**",
        "",
        "| Status | Count | % |",
        "|--------|-------|---|",
        status_rows,
    ]

    if recent:
        lines += [
            "",
            "### Recent (last 20)",
            "",
            "| Time | User | Status | Type | Preview |",
            "|------|------|--------|------|---------|",
        ]
        for n in recent:
            created = n.created_at.strftime("%m/%d %H:%M") if n.created_at else "?"
            preview = _esc(
                (n.message_text[:_PREVIEW_TRUNCATE_LEN] + "...")
                if len(n.message_text) > _PREVIEW_TRUNCATE_LEN
                else n.message_text
            )
            lines.append(
                f"| {created} | {n.user_id} | {_status_badge(n.status)} "
                f"| {n.notification_type} | {preview} |"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System tab
# ---------------------------------------------------------------------------

async def get_system_health() -> str:
    """Get system health information as Markdown."""
    lines = ["### Infrastructure", ""]

    # Redis
    try:
        redis = await get_redis()
        info = await redis.info("memory")
        clients_info = await redis.info("clients")
        redis_mem = info.get("used_memory_human", "N/A")
        redis_clients = clients_info.get("connected_clients", "N/A")
        lines.append(
            f'- <span class="health-ok">OK</span> **Redis** &mdash; '
            f"memory: {redis_mem}, clients: {redis_clients}"
        )
    except Exception:
        lines.append('- <span class="health-alert">DOWN</span> **Redis** &mdash; unavailable')

    # Database
    try:
        async with async_session_factory() as db:
            user_count = await UserRepo.count(db)
            total_users = await UserRepo.count(db, active_only=False)
        lines.append(
            f'- <span class="health-ok">OK</span> **Database** &mdash; '
            f"{user_count} active users ({total_users} total)"
        )
    except Exception:
        lines.append('- <span class="health-alert">DOWN</span> **Database** &mdash; unavailable')

    # Session pool
    try:
        int_active = session_pool.interactive_active
        int_max = settings.max_concurrent_interactive_sessions
        pro_active = session_pool.proactive_active
        pro_max = settings.max_concurrent_proactive_sessions
        lines += [
            "",
            f"""\
<div class="stat-row">
<div class="stat-card"><div class="stat-value">{int_active}/{int_max}</div><div class="stat-label">Interactive pool</div></div>
<div class="stat-card"><div class="stat-value">{pro_active}/{pro_max}</div><div class="stat-label">Proactive pool</div></div>
</div>""",
        ]
    except Exception:
        lines.append("- **Session pool:** unavailable")

    # Configuration
    try:
        parsed = urlparse(settings.redis_url)
        redis_display = f"{parsed.hostname}:{parsed.port or 6379}/{parsed.path.lstrip('/') or '0'}"
    except Exception:
        redis_display = "(configured)"

    lines += [
        "",
        "### Configuration",
        "",
        "| Setting | Value |",
        "|---------|-------|",
        f"| Proactive tick | {settings.proactive_tick_interval_seconds}s |",
        "| Free model | claude-haiku-4-5 |",
        "| Premium model | claude-sonnet-4-6 |",
        f"| DB host | {settings.postgres_host}:{settings.postgres_port} |",
        f"| Redis | {redis_display} |",
        "",
        f"*Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*",
    ]

    return "\n".join(lines)


async def get_health_alerts_display() -> str:
    """Get health alert status as Markdown."""
    try:
        status = await get_health_status()
        lines = ["### Health Checks", ""]
        for check_name, check_status in status.items():
            lines.append(f"- {_health_icon(check_status)} **{check_name}:** {check_status}")
        lines += [
            "",
            f"*Checked at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"**Error fetching health status:** {e}"


# ---------------------------------------------------------------------------
# Composite loaders for auto-load
# ---------------------------------------------------------------------------

async def _load_all_costs() -> tuple[str, list[list], list[list]]:
    summary, daily, per_user = await asyncio.gather(
        get_cost_summary(), get_cost_data(), get_cost_per_user(),
    )
    return summary, daily, per_user


async def _load_all_alerts() -> tuple[list[list], str]:
    failures, notif_stats = await asyncio.gather(
        get_pipeline_failures(), get_notification_stats(),
    )
    return failures, notif_stats


async def _load_system_data() -> tuple[str, str]:
    health, alerts = await asyncio.gather(
        get_system_health(), get_health_alerts_display(),
    )
    return health, alerts


# ---------------------------------------------------------------------------
# Build Gradio app
# ---------------------------------------------------------------------------

def create_admin_app() -> gr.Blocks:
    """Create the Gradio admin dashboard."""

    with gr.Blocks(title="LangBot Admin") as app:

        # --- Header ---
        gr.HTML(
            '<div class="admin-header">'
            "<h1>LangBot Admin Dashboard</h1>"
            "<p>System monitoring &amp; user management</p>"
            "</div>"
        )

        # --- Users tab ---
        with gr.Tab("Users") as users_tab:
            with gr.Row():
                search_input = gr.Textbox(
                    label="Search (name, username, or Telegram ID)",
                    placeholder="Type to search...",
                    scale=3,
                )
                search_btn = gr.Button("Search", scale=1, variant="primary")
            users_table = gr.Dataframe(
                headers=_USER_HEADERS,
                label="Users",
                show_search="search",
                max_height=500,
                interactive=False,
                column_widths=[
                    "90px", "120px", "110px", "60px", "55px",
                    "55px", "65px", "75px", "100px", "50px", "50px",
                ],
            )
            refresh_users_btn = gr.Button("Reload All Users", variant="secondary", size="sm")
            search_btn.click(search_users, inputs=search_input, outputs=users_table)
            refresh_users_btn.click(get_users_data, outputs=users_table)

            with gr.Accordion("User Actions", open=False):
                action_user_id = gr.Number(label="Telegram User ID", precision=0)
                with gr.Row():
                    tier_btn = gr.Button("Toggle Tier", variant="secondary", size="sm")
                    active_btn = gr.Button("Toggle Active", variant="secondary", size="sm")
                    admin_btn = gr.Button("Toggle Admin", variant="secondary", size="sm")
                action_status = gr.Markdown()
                gr.Markdown("---")
                detail_btn = gr.Button("View Full Profile", variant="primary")
                user_detail_md = gr.Markdown(
                    sanitize_html=False, elem_classes=["report-md"],
                )
            tier_btn.click(toggle_user_tier, inputs=action_user_id, outputs=action_status)
            active_btn.click(toggle_user_active, inputs=action_user_id, outputs=action_status)
            admin_btn.click(toggle_user_admin, inputs=action_user_id, outputs=action_status)
            detail_btn.click(get_user_detail, inputs=action_user_id, outputs=user_detail_md)

        # --- Sessions tab ---
        with gr.Tab("Sessions") as sessions_tab:
            sessions_table = gr.Dataframe(
                headers=["ID", "User", "Type", "Cost", "Turns",
                         "Tools", "Pipeline", "Started", "Duration(ms)"],
                label="Recent Sessions (last 100)",
                show_search="search",
                max_height=500,
                interactive=False,
            )
            refresh_sessions_btn = gr.Button("Refresh", variant="secondary", size="sm")
            refresh_sessions_btn.click(get_sessions_data, outputs=sessions_table)

        # --- Costs tab ---
        with gr.Tab("Costs") as costs_tab:
            cost_summary_md = gr.Markdown(
                sanitize_html=False, elem_classes=["report-md"],
            )
            with gr.Row(equal_height=True):
                with gr.Column(scale=1):
                    gr.Markdown("### Daily Breakdown (14 days)")
                    costs_table = gr.Dataframe(
                        headers=["Date", "Total Cost"],
                        label="Daily Costs",
                        max_height=420,
                        interactive=False,
                    )
                with gr.Column(scale=1):
                    gr.Markdown("### Per-User Costs (7 days)")
                    user_costs_table = gr.Dataframe(
                        headers=["User ID", "Name", "Total Cost", "Sessions"],
                        label="Cost per User",
                        max_height=420,
                        interactive=False,
                    )
            refresh_costs_btn = gr.Button("Refresh All Cost Data", variant="primary", size="sm")
            refresh_costs_btn.click(
                _load_all_costs,
                outputs=[cost_summary_md, costs_table, user_costs_table],
            )

        # --- Alerts tab ---
        with gr.Tab("Alerts") as alerts_tab:
            with gr.Row(equal_height=True):
                with gr.Column(scale=1):
                    gr.Markdown("### Pipeline Failures")
                    failures_table = gr.Dataframe(
                        headers=["Session ID", "User", "Type", "Status",
                                 "Issues", "Started"],
                        label="Pipeline failures (failed/pending)",
                        max_height=500,
                        interactive=False,
                    )
                with gr.Column(scale=1):
                    gr.Markdown("### Notification Stats")
                    notif_stats_md = gr.Markdown(
                        sanitize_html=False, elem_classes=["report-md"],
                    )
            refresh_alerts_btn = gr.Button("Refresh Alerts", variant="primary", size="sm")
            refresh_alerts_btn.click(
                _load_all_alerts,
                outputs=[failures_table, notif_stats_md],
            )

        # --- System tab ---
        with gr.Tab("System") as system_tab:
            with gr.Row(equal_height=True):
                with gr.Column(scale=3):
                    health_md = gr.Markdown(
                        sanitize_html=False, elem_classes=["report-md"],
                    )
                with gr.Column(scale=2):
                    alerts_status_md = gr.Markdown(
                        sanitize_html=False, elem_classes=["report-md"],
                    )
            refresh_system_btn = gr.Button("Refresh System Status", variant="primary", size="sm")
            refresh_system_btn.click(
                _load_system_data,
                outputs=[health_md, alerts_status_md],
            )

        # --- Auto-load on tab selection ---
        users_tab.select(get_users_data, outputs=users_table)
        sessions_tab.select(get_sessions_data, outputs=sessions_table)
        costs_tab.select(
            _load_all_costs,
            outputs=[cost_summary_md, costs_table, user_costs_table],
        )
        alerts_tab.select(
            _load_all_alerts,
            outputs=[failures_table, notif_stats_md],
        )
        system_tab.select(
            _load_system_data,
            outputs=[health_md, alerts_status_md],
        )

        # Load default tab (Users) on app open
        app.load(get_users_data, outputs=users_table)

    return app
