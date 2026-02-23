from html import escape as esc

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.config import TIER_LIMITS, UserTier
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import ExerciseResultRepo, VocabularyRepo
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, get_localized_language_name, render_goal, t

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

    scores = user.recent_scores or []
    recent_5 = scores[-5:] if scores else []
    avg = sum(recent_5) / len(recent_5) if recent_5 else 0

    scores_display = ", ".join(str(s) for s in recent_5) if recent_5 else t("stats.no_scores", lang)

    target_lang_name = esc(get_localized_language_name(user.target_language, lang))

    lines = [
        t("stats.title", lang, target_language=target_lang_name),
        t("stats.level", lang, level=esc(user.level)),
        t("stats.streak", lang, days=user.streak_days),
        t("stats.vocabulary", lang, count=user.vocabulary_count),
        t("stats.sessions", lang, count=user.sessions_completed),
        t("stats.cards_due", lang, count=due_count),
        t("stats.difficulty", lang, value=esc(user.preferred_difficulty)),
        t("stats.style", lang, value=esc(user.session_style)),
        t("stats.recent_scores", lang, scores=scores_display),
    ]

    if recent_5:
        lines.append(t("stats.average", lang, avg=f"{avg:.1f}"))

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

    if recent:
        lines.append(t("stats.recent_exercises", lang))
        for ex in recent:
            lines.append(
                t("stats.exercise_line", lang,
                  topic=esc(ex.topic), score=ex.score,
                  max_score=ex.max_score, exercise_type=esc(ex.exercise_type)),
            )

    # Tier info
    try:
        tier = UserTier(user.tier)
    except ValueError:
        tier = UserTier.FREE
    limits = TIER_LIMITS[tier]

    lines.append(t("stats.tier", lang, tier=tier.value))
    if limits.max_sessions_per_day > 0:
        lines.append(t("stats.sessions_per_day", lang, count=limits.max_sessions_per_day))
    else:
        lines.append(t("stats.sessions_unlimited", lang))
    lines.append(t("stats.messages_per_session", lang, count=limits.max_turns_per_session))

    await message.answer("\n".join(lines))
