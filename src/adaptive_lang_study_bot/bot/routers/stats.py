from datetime import datetime, timezone
from html import escape as esc

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.agent.tools import compute_plan_progress, fetch_plan_topic_stats
from adaptive_lang_study_bot.bot.helpers import localize_value
from adaptive_lang_study_bot.config import TIER_LIMITS, UserTier
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import ExerciseResultRepo, LearningPlanRepo, VocabularyRepo
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, get_localized_language_name, render_goal, t
from adaptive_lang_study_bot.utils import compute_level_progress, score_label

router = Router()


@router.message(Command("stats"))
async def cmd_stats(message: Message, user: User, db_session: AsyncSession) -> None:
    lang = user.native_language or DEFAULT_LANGUAGE
    if not user.onboarding_completed:
        await message.answer(t("stats.setup_first", lang))
        return

    due_count = await VocabularyRepo.count_due(db_session, user.telegram_id)
    recent = await ExerciseResultRepo.get_recent(
        db_session, user.telegram_id, limit=5,
    )

    target_lang_name = esc(get_localized_language_name(user.target_language, lang))

    lines = [
        t("stats.title", lang, target_language=target_lang_name),
        t("stats.level", lang, level=esc(user.level)),
        t("stats.streak", lang, days=user.streak_days),
        t("stats.vocabulary", lang, count=user.vocabulary_count),
        t("stats.sessions", lang, count=user.sessions_completed),
        t("stats.cards_due", lang, count=due_count),
        t("stats.difficulty", lang, value=esc(localize_value(user.preferred_difficulty, lang))),
        t("stats.style", lang, value=esc(localize_value(user.session_style, lang))),
    ]

    # Learning goals
    if user.learning_goals:
        lines.append(t("stats.goals_title", lang))
        target_name = get_localized_language_name(user.target_language, lang)
        for i, goal in enumerate(user.learning_goals, 1):
            lines.append(f"  {i}. {esc(render_goal(goal, lang, target_language=target_name))}")
    else:
        lines.append(t("stats.no_goals", lang))

    if user.weak_areas:
        weak = ", ".join(esc(a) for a in user.weak_areas)
        lines.append(t("stats.weak_areas", lang, areas=weak))
    if user.strong_areas:
        strong = ", ".join(esc(a) for a in user.strong_areas)
        lines.append(t("stats.strong_areas", lang, areas=strong))

    # Level progress
    if user.recent_scores:
        level_progress = compute_level_progress(user.recent_scores, user.level)
        lines.append(t("stats.level_progress", lang, progress=esc(level_progress)))

    # Learning plan
    plan = await LearningPlanRepo.get_active(db_session, user.telegram_id)
    if plan:
        today = datetime.now(timezone.utc).date()
        elapsed_days = (today - plan.start_date).days
        current_week = max(1, min(plan.total_weeks, elapsed_days // 7 + 1))

        # Compute progress from exercise results
        topic_stats_raw = await fetch_plan_topic_stats(
            db_session, user.telegram_id, plan,
        )
        progress = compute_plan_progress(
            plan.plan_data or {}, plan.total_weeks, plan.start_date, topic_stats_raw,
        )

        lines.append(t(
            "stats.plan_title", lang,
            current_level=esc(plan.current_level),
            target_level=esc(plan.target_level),
        ))
        lines.append(t(
            "stats.plan_progress", lang,
            completed=progress["completed_topics"],
            total=progress["total_topics"],
            pct=progress["progress_pct"],
        ))
        lines.append(t(
            "stats.plan_week", lang,
            current=current_week,
            total=plan.total_weeks,
        ))

        # Current phase info
        for phase in progress["phases"]:
            if phase["status"] == "in_progress":
                lines.append(t(
                    "stats.plan_current_phase", lang,
                    focus=esc(phase["focus"] or ""),
                ))
                break

    if recent:
        lines.append(t("stats.recent_exercises", lang))
        for ex in recent:
            label = score_label(ex.score / ex.max_score * 10 if ex.max_score else ex.score)
            lines.append(
                t("stats.exercise_line", lang,
                  topic=esc(ex.topic), label=esc(label),
                  exercise_type=esc(ex.exercise_type)),
            )

    # Tier info
    try:
        tier = UserTier(user.tier)
    except ValueError:
        tier = UserTier.FREE
    limits = TIER_LIMITS[tier]

    lines.append(t("stats.tier", lang, tier=localize_value(tier.value, lang)))
    if limits.max_sessions_per_day > 0:
        lines.append(t("stats.sessions_per_day", lang, count=limits.max_sessions_per_day))
    else:
        lines.append(t("stats.sessions_unlimited", lang))
    lines.append(t("stats.messages_per_session", lang, count=limits.max_turns_per_session))

    await message.answer("\n".join(lines))
