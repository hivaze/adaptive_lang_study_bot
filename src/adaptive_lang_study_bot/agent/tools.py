import asyncio
import json
import uuid as _uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from loguru import logger
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from dateutil.rrule import DAILY, HOURLY, MINUTELY, MONTHLY, SECONDLY, WEEKLY, YEARLY, rrulestr

from adaptive_lang_study_bot.config import CEFR_LEVELS, TIER_LIMITS, settings, tuning

try:
    from tavily import AsyncTavilyClient
    _TAVILY_AVAILABLE = True
except ImportError:
    _TAVILY_AVAILABLE = False
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    LearningPlanRepo,
    ScheduleRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
    VocabularyReviewLogRepo,
)
from adaptive_lang_study_bot.enums import (
    Difficulty,
    NotificationTier,
    ScheduleType,
    SessionStyle,
    SessionType,
    UserTier,
)
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.fsrs_engine.scheduler import create_new_card, review_card
from adaptive_lang_study_bot.utils import compute_next_trigger, safe_zoneinfo, score_label, stamp_field, strip_mcp_prefix

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_MUTABLE_FIELDS = {"interests", "learning_goals", "preferred_difficulty", "session_style", "topics_to_avoid", "notifications_paused", "additional_notes"}


async def _record_plan_completion(
    db: AsyncSession,
    user_id: int,
    current_level: str,
    target_level: str,
) -> None:
    """Stamp plan completion in milestones JSONB for long-term history."""
    user = await UserRepo.get(db, user_id)
    if not user:
        return
    milestones = dict(user.milestones or {})
    completed: list[dict] = milestones.get("completed_plans", [])
    completed.append({
        "from": current_level,
        "to": target_level,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    milestones["completed_plans"] = completed[-20:]  # cap history
    await UserRepo.update_milestones(db, user_id, milestones)


def web_search_available() -> bool:
    """Check if web search tool can be created (library installed + API key set)."""
    return _TAVILY_AVAILABLE and bool(settings.tavily_api_key)

# Base period in minutes for each dateutil frequency constant.
# Uses private _freq / _interval attrs — stable across all dateutil versions.
_FREQ_BASE_MINUTES: dict[int, float] = {
    YEARLY: 525960, MONTHLY: 43200, WEEKLY: 10080,
    DAILY: 1440, HOURLY: 60, MINUTELY: 1, SECONDLY: 1 / 60,
}


def compute_plan_progress(
    plan_data: dict,
    total_weeks: int,
    start_date: date,
    topic_stats: dict[str, dict],
) -> dict:
    """Derive learning plan progress from ExerciseResult data.

    Called by the manage_learning_plan tool and by prompt_builder / proactive
    triggers to compute plan progress without storing per-topic state.
    """
    phases = plan_data.get("phases", [])

    completed_topics = 0
    total_topics = 0
    phase_results: list[dict] = []

    for phase in phases:
        topics = phase.get("topics", [])
        total_topics += len(topics)
        topic_details: list[dict] = []
        phase_completed = 0

        for topic_name in topics:
            stats = topic_stats.get(topic_name, {})
            count = stats.get("count", 0)
            avg_score = stats.get("avg_score")
            last_practiced = stats.get("last_practiced")

            is_consolidation = phase.get("consolidation", False)
            mastery = (
                tuning.consolidation_mastery_score if is_consolidation
                else tuning.plan_topic_mastery_score
            )
            if (
                count >= tuning.plan_topic_min_exercises
                and avg_score is not None
                and avg_score >= mastery
            ):
                status = "completed"
                phase_completed += 1
                completed_topics += 1
            elif count > 0:
                status = "in_progress"
            else:
                status = "pending"

            detail: dict[str, Any] = {
                "name": topic_name,
                "status": status,
                "exercises": count,
            }
            if avg_score is not None:
                detail["avg_score"] = avg_score
            if last_practiced:
                detail["last_practiced"] = last_practiced
            topic_details.append(detail)

        phase_status = "completed" if phase_completed == len(topics) and topics else (
            "in_progress" if phase_completed > 0 or any(
                t["status"] == "in_progress" for t in topic_details
            ) else "pending"
        )
        result_entry: dict[str, Any] = {
            "week": phase.get("week"),
            "focus": phase.get("focus"),
            "status": phase_status,
            "topics": topic_details,
        }
        if phase.get("consolidation"):
            result_entry["consolidation"] = True
        phase_results.append(result_entry)

    progress_pct = round(completed_topics / total_topics * 100) if total_topics > 0 else 0

    return {
        "progress_pct": progress_pct,
        "completed_topics": completed_topics,
        "total_topics": total_topics,
        "phases": phase_results,
    }


async def fetch_plan_topic_stats(
    db: AsyncSession,
    user_id: int,
    plan: "LearningPlan",
) -> dict[str, dict]:
    """Fetch per-topic exercise stats, using split date ranges for consolidation phases.

    Regular topics use plan.start_date.  Consolidation topics (added after
    plan hit 100%) use the consolidation_added_at date so only fresh
    exercises count toward their mastery.
    """
    plan_data = plan.plan_data or {}
    consol_since_str = plan_data.get("consolidation_added_at")

    if not consol_since_str:
        all_topics = [
            t for p in plan_data.get("phases", []) for t in p.get("topics", [])
        ]
        return await ExerciseResultRepo.get_stats_for_topics(
            db, user_id, all_topics, plan.start_date,
        )

    regular_topics: list[str] = []
    consolidation_topics: list[str] = []
    for p in plan_data.get("phases", []):
        bucket = consolidation_topics if p.get("consolidation") else regular_topics
        bucket.extend(p.get("topics", []))

    stats = await ExerciseResultRepo.get_stats_for_topics(
        db, user_id, regular_topics, plan.start_date,
    )
    if consolidation_topics:
        consol_stats = await ExerciseResultRepo.get_stats_for_topics(
            db, user_id, consolidation_topics,
            date.fromisoformat(consol_since_str),
        )
        stats.update(consol_stats)
    return stats


async def _maybe_add_consolidation_phase(
    db: AsyncSession,
    plan: "LearningPlan",
    progress: dict,
    topic_stats: dict[str, dict],
    user_level: str,
) -> bool:
    """Auto-extend plan with a consolidation phase when 100% complete but level not reached.

    Returns True if a consolidation phase was added.
    """
    plan_data = plan.plan_data or {}

    # Guards
    if plan_data.get("consolidation_added"):
        return False
    if progress["progress_pct"] < 100:
        return False
    if plan.total_weeks >= tuning.plan_max_weeks:
        return False

    target_level = plan.target_level
    if target_level in CEFR_LEVELS and user_level in CEFR_LEVELS:
        if CEFR_LEVELS.index(user_level) >= CEFR_LEVELS.index(target_level):
            return False  # already at or above target
    else:
        return False

    # Find weakest completed topics (avg_score below consolidation mastery)
    weak_topics: list[tuple[str, float]] = []
    for phase_info in progress["phases"]:
        if phase_info.get("consolidation"):
            continue
        for t in phase_info.get("topics", []):
            if t["status"] == "completed":
                avg = topic_stats.get(t["name"], {}).get("avg_score")
                if avg is not None and avg < tuning.consolidation_mastery_score:
                    weak_topics.append((t["name"], avg))

    if not weak_topics:
        return False  # all topics already at level-up threshold

    # Pick weakest topics, capped at plan_max_topics_per_week
    weak_topics.sort(key=lambda x: x[1])
    selected = [name for name, _ in weak_topics[:tuning.plan_max_topics_per_week]]

    # Build consolidation phase
    new_week = plan.total_weeks + 1
    phase_start = plan.start_date + timedelta(weeks=new_week - 1)
    phase_end = plan.start_date + timedelta(weeks=new_week) - timedelta(days=1)
    today_str = datetime.now(timezone.utc).date().isoformat()

    consolidation_phase = {
        "week": new_week,
        "start_date": phase_start.isoformat(),
        "end_date": phase_end.isoformat(),
        "focus": f"Consolidation — strengthen weak areas for {target_level}",
        "topics": selected,
        "consolidation": True,
    }

    phases = list(plan_data.get("phases", []))
    phases.append(consolidation_phase)
    plan_data = dict(plan_data)
    plan_data["phases"] = phases
    plan_data["consolidation_added"] = True
    plan_data["consolidation_added_at"] = today_str

    new_end_date = plan.start_date + timedelta(weeks=new_week)
    await LearningPlanRepo.update_fields(
        db,
        plan.id,
        plan_data=plan_data,
        total_weeks=new_week,
        target_end_date=new_end_date,
    )
    await db.commit()
    return True


def _rrule_interval_minutes(rrule_str: str) -> float:
    """Compute effective recurrence interval in minutes from an RRULE string."""
    rule = rrulestr(rrule_str)
    base = _FREQ_BASE_MINUTES.get(rule._freq)
    if base is None:
        raise ValueError(f"Unknown RRULE frequency: {rule._freq}")
    return base * rule._interval


# Tool names with MCP prefix, used for allowed_tools config
TOOL_NAMES = [
    "mcp__langbot__get_user_profile",
    "mcp__langbot__update_preference",
    "mcp__langbot__record_exercise_result",
    "mcp__langbot__add_vocabulary",
    "mcp__langbot__get_due_vocabulary",
    "mcp__langbot__manage_schedule",
    "mcp__langbot__search_vocabulary",
    "mcp__langbot__get_exercise_history",
    "mcp__langbot__get_progress_summary",
    "mcp__langbot__manage_learning_plan",
    "mcp__langbot__adjust_level",
    "mcp__langbot__web_search",
    "mcp__langbot__web_extract",
]

# Tools allowed per session type
_SESSION_TYPE_TOOLS: dict[SessionType, set[str]] = {
    SessionType.INTERACTIVE: {
        "get_user_profile", "update_preference", "record_exercise_result",
        "add_vocabulary", "get_due_vocabulary",
        "manage_schedule", "search_vocabulary", "get_exercise_history",
        "get_progress_summary", "manage_learning_plan", "adjust_level",
        "end_session",
        "web_search", "web_extract",
    },
    SessionType.ONBOARDING: {
        "get_user_profile", "update_preference", "record_exercise_result",
        "add_vocabulary", "search_vocabulary", "manage_schedule",
        "adjust_level", "end_session",
    },
    SessionType.PROACTIVE_REVIEW: {"web_search", "web_extract"},
    SessionType.PROACTIVE_QUIZ: {"web_search", "web_extract"},
    SessionType.PROACTIVE_SUMMARY: {"web_search", "web_extract"},
    SessionType.PROACTIVE_NUDGE: {"web_search", "web_extract"},
    SessionType.ASSESSMENT: set(),
}


def _score_to_fsrs_rating(normalized_score: int) -> int:
    """Map normalized exercise score (0-10) to FSRS rating (1=Again, 2=Hard, 3=Good, 4=Easy)."""
    if normalized_score <= tuning.fsrs_rating_fail_threshold:
        return 1
    if normalized_score <= tuning.fsrs_rating_hard_threshold:
        return 2
    if normalized_score >= tuning.fsrs_rating_easy_threshold:
        return 4
    return 3


def _validate_and_compute_rrule(
    rrule_str: str, user_tz,
) -> tuple[datetime | None, str | None]:
    """Validate RRULE interval and compute next trigger.

    Returns ``(next_trigger, None)`` on success, or ``(None, error_msg)`` on failure.
    """
    try:
        interval = _rrule_interval_minutes(rrule_str)
        if interval < tuning.min_schedule_interval_minutes:
            return None, (
                f"Schedule fires too frequently (~{int(interval)} min). "
                f"Minimum interval is {tuning.min_schedule_interval_minutes} minutes. "
                f"Use FREQ=HOURLY or less frequent."
            )
    except (ValueError, TypeError):
        pass  # compute_next_trigger below provides the error

    try:
        next_trigger = compute_next_trigger(rrule_str, user_tz)
    except (ValueError, TypeError) as e:
        return None, f"Invalid RRULE: {e}"

    if next_trigger is None:
        return None, "RRULE produces no future occurrences"

    return next_trigger, None


def _estimate_daily_llm_notifications(
    existing_schedules: list, new_rrule: str | None = None,
) -> float:
    """Estimate daily LLM notification count from existing schedules plus an optional new one."""
    estimate = 0.0
    for s in existing_schedules:
        if s.notification_tier in (NotificationTier.LLM, NotificationTier.HYBRID):
            try:
                s_interval = _rrule_interval_minutes(s.rrule)
                estimate += max(1, 1440 / s_interval)
            except (ValueError, TypeError):
                estimate += 1
    if new_rrule:
        try:
            new_interval = _rrule_interval_minutes(new_rrule)
            estimate += max(1, 1440 / new_interval)
        except (ValueError, TypeError):
            estimate += 1
    return estimate


def _parse_list_field(raw_value: Any, *, max_items: int, max_len: int) -> list[str]:
    """Parse a raw value into a bounded, trimmed list of strings.

    Handles: JSON arrays, semicolon-delimited strings, comma-delimited
    strings, and plain scalar values.  The agent often sends delimited
    strings instead of JSON arrays for list fields.
    """
    try:
        value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except json.JSONDecodeError:
        # Fallback: split on semicolons or commas (agent often sends these)
        if isinstance(raw_value, str) and (";" in raw_value or "," in raw_value):
            sep = ";" if ";" in raw_value else ","
            value = [s.strip() for s in raw_value.split(sep) if s.strip()]
        else:
            value = [raw_value]
    if not isinstance(value, list):
        value = [value]
    return [str(v).strip()[:max_len] for v in value[:max_items]]


# ---------------------------------------------------------------------------
# Per-session tool factory
# ---------------------------------------------------------------------------

def create_session_tools(
    session_factory: Callable,
    user_id: int,
    session_id: str | None = None,
    session_type: SessionType = SessionType.INTERACTIVE,
    user_timezone: str = "UTC",
    user_tier: str = UserTier.FREE,
) -> tuple[list, Callable[[str], bool]]:
    """Create tool functions with per-session state captured via closures.

    Each session gets its own set of tool functions that reference the
    session factory and user ID through closure capture.  DB connections
    are acquired per tool call (not held for the entire session lifetime)
    to minimize pool pressure.

    Returns:
        Tuple of (tools_list, can_use_tool_fn)
    """

    def _ok(data: dict) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}

    def _err(msg: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": msg}], "is_error": True}

    def _session_uuid() -> _uuid.UUID | None:
        if session_id:
            try:
                return _uuid.UUID(session_id)
            except (ValueError, TypeError):
                pass
        return None

    _user_tz = safe_zoneinfo(user_timezone)

    def _to_local_iso(dt):
        """Convert a timezone-aware datetime to user-local ISO string."""
        if dt is None:
            return None
        return dt.astimezone(_user_tz).isoformat()

    # --- Tool definitions (each captures session_factory, user_id via closure) ---

    @tool(
        "get_user_profile",
        "Load the student's full learning profile including stats, "
        "weak/strong areas, preferences, and streak.",
        {},
    )
    async def get_user_profile(args: dict[str, Any]) -> dict[str, Any]:
        async with session_factory() as db_session:
            user = await UserRepo.get(db_session, user_id)
            if user is None:
                return _err("User not found")
            due_count = await VocabularyRepo.count_due(db_session, user_id)
            profile = {
                "telegram_id": user.telegram_id,
                "first_name": user.first_name,
                "native_language": user.native_language,
                "target_language": user.target_language,
                "level": user.level,
                "interests": user.interests or [],
                "learning_goals": user.learning_goals or [],
                "preferred_difficulty": user.preferred_difficulty,
                "session_style": user.session_style,
                "topics_to_avoid": user.topics_to_avoid or [],
                "weak_areas": user.weak_areas or [],
                "strong_areas": user.strong_areas or [],
                "recent_scores": user.recent_scores or [],
                "vocabulary_count": user.vocabulary_count,
                "streak_days": user.streak_days,
                "sessions_completed": user.sessions_completed,
                "last_activity": user.last_activity,
                "session_history": user.session_history or [],
                "pending_reviews": due_count,
                "due_fraction": round(due_count / max(user.vocabulary_count, 1), 2),
                "tier": user.tier,
                "notifications_paused": user.notifications_paused,
                "field_timestamps": user.field_timestamps or {},
            }
            return _ok(profile)

    @tool(
        "update_preference",
        "Update a user preference. Allowed fields: interests (list of up to 8), "
        "learning_goals (list of up to 5 current learning goals), "
        "preferred_difficulty (easy/normal/hard), session_style (casual/structured/intensive), "
        "topics_to_avoid (list of up to 5), "
        "notifications_paused (true/false — pause or resume all proactive notifications), "
        "additional_notes (list of up to 10 observations about the student's preferences "
        "and behavior, e.g. 'prefers vocab before exercises', 'enjoys role-play'). "
        "Use learning_goals to record what the student is working toward, e.g. "
        "'Prepare for DELF B2 exam', 'Learn cooking vocabulary for trip to France'. "
        "For list fields, set mode='append' (default) to add items to the existing "
        "list, or mode='replace' to replace the entire list.",
        {"field": str, "value": str, "mode": str},
    )
    async def update_preference(args: dict[str, Any]) -> dict[str, Any]:
        field = args["field"]
        raw_value = args["value"]
        mode = args.get("mode", "append")

        if field not in _USER_MUTABLE_FIELDS:
            return _err(
                f"Cannot update '{field}'. Allowed: {', '.join(sorted(_USER_MUTABLE_FIELDS))}",
            )

        if field == "interests":
            value = _parse_list_field(raw_value, max_items=tuning.max_interests, max_len=tuning.max_interest_item_length)
        elif field == "topics_to_avoid":
            value = _parse_list_field(raw_value, max_items=tuning.max_topics_to_avoid, max_len=tuning.max_topic_to_avoid_item_length)
        elif field == "learning_goals":
            value = _parse_list_field(raw_value, max_items=tuning.max_learning_goals, max_len=tuning.max_goal_item_length)
        elif field == "preferred_difficulty":
            value = raw_value.lower().strip()
            if value not in Difficulty:
                return _err(f"Invalid difficulty '{value}'. Use: {', '.join(Difficulty)}")
        elif field == "session_style":
            value = raw_value.lower().strip()
            if value not in SessionStyle:
                return _err(f"Invalid style '{value}'. Use: {', '.join(SessionStyle)}")
        elif field == "additional_notes":
            value = _parse_list_field(raw_value, max_items=tuning.max_additional_notes, max_len=tuning.max_note_item_length)
        elif field == "notifications_paused":
            if isinstance(raw_value, bool):
                value = raw_value
            elif isinstance(raw_value, str):
                value = raw_value.lower().strip() in ("true", "1", "yes", "on")
            else:
                return _err("notifications_paused must be true or false")
        else:
            value = raw_value

        async with session_factory() as db_session:
            try:
                user = await UserRepo.get(db_session, user_id)
                if user is None:
                    return _err("User not found")

                # For list fields in append mode, merge with existing values
                if isinstance(value, list) and mode == "append":
                    existing = getattr(user, field, None) or []
                    # Case-insensitive dedup preserving order (existing first)
                    seen = {item.lower() for item in existing}
                    merged = list(existing)
                    for item in value:
                        if item.lower() not in seen:
                            merged.append(item)
                            seen.add(item.lower())
                    value = merged

                date_str = datetime.now(_user_tz).strftime("%Y-%m-%d")
                ts = stamp_field(user.field_timestamps, field, value, date_str)
                await UserRepo.update_fields(
                    db_session, user_id, field_timestamps=ts, **{field: value},
                )
                await db_session.commit()
            except SQLAlchemyError:
                logger.exception("Failed to update preference for user {}", user_id)
                return _err("Database error updating preference, please try again")
        logger.info("User {} updated {}: {}", user_id, field, value)
        return _ok({"status": "updated", "field": field, "new_value": value})

    @tool(
        "record_exercise_result",
        "Record the result of a learning exercise after the student answers it. "
        "Must be called ONLY AFTER the student provides their answer — never before. "
        "If the student ignores an exercise, do NOT record a score. "
        "Auto-adjusts weak/strong areas based on scores. "
        "Words listed in words_involved are automatically reviewed in the spaced "
        "repetition system (FSRS) — always include the vocabulary words used in the exercise.",
        {"exercise_type": str, "topic": str, "score": int, "max_score": int,
         "words_involved": str, "notes": str},
    )
    async def record_exercise_result(args: dict[str, Any]) -> dict[str, Any]:
        score = args["score"]
        max_score = args.get("max_score", 10)

        if not (1 <= max_score <= 10):
            return _err("max_score must be between 1 and 10")

        if not (0 <= score <= max_score):
            return _err(f"Score must be between 0 and {max_score}")

        # Normalize to 0-10 scale for consistent level/area thresholds
        normalized_score = round(score * 10 / max_score) if max_score != 10 else score
        exercise_type = str(args["exercise_type"]).strip()[:tuning.max_exercise_type_length]
        topic = str(args["topic"]).strip()[:tuning.max_topic_length]

        if not exercise_type:
            return _err("exercise_type must not be empty")
        if not topic:
            return _err("topic must not be empty")

        words_raw = args.get("words_involved", "[]")
        try:
            words = json.loads(words_raw) if isinstance(words_raw, str) else words_raw
        except json.JSONDecodeError:
            # Agent sent a plain string (e.g. "le chat, un chien") instead of
            # a JSON array.  Split on commas so each word is stored individually.
            words = [w.strip() for w in words_raw.split(",") if w.strip()] if words_raw else []
        if not isinstance(words, list):
            words = [words]
        words = [w for w in (str(w).strip()[:tuning.max_word_length] for w in words[:tuning.max_exercise_words]) if w]

        async with session_factory() as db_session:
            try:
                await ExerciseResultRepo.create(
                    db_session,
                    user_id=user_id,
                    session_id=_session_uuid(),
                    exercise_type=exercise_type,
                    topic=topic,
                    score=score,
                    max_score=max_score,
                    words_involved=words if words else None,
                    agent_notes=args.get("notes"),
                )

                scores = await UserRepo.append_score(db_session, user_id, normalized_score)
                adjustments: list[str] = []

                user = await UserRepo.get(db_session, user_id)
                if user is None:
                    return _err("User not found")

                # Running field_timestamps dict — updated progressively
                ts = dict(user.field_timestamps or {})
                date_str = datetime.now(_user_tz).strftime("%Y-%m-%d")

                # Weak/strong area adjustment — requires multiple qualifying
                # scores for the same topic before modifying areas.
                weak = list(user.weak_areas or [])
                strong = list(user.strong_areas or [])

                recent_topic_results = await ExerciseResultRepo.get_recent(
                    db_session, user_id, topic=topic, limit=tuning.weak_strong_recent_limit,
                )
                recent_topic_scores = [r.score for r in recent_topic_results]

                strong_count = sum(
                    1 for s in recent_topic_scores if s >= tuning.strong_area_score
                )
                weak_count = sum(
                    1 for s in recent_topic_scores if s <= tuning.weak_area_score
                )

                if strong_count >= tuning.strong_area_min_occurrences and topic in weak:
                    weak.remove(topic)
                    if topic not in strong:
                        strong.append(topic)
                    ts = stamp_field(ts, "weak_areas", weak, date_str)
                    ts = stamp_field(ts, "strong_areas", strong, date_str)
                    await UserRepo.update_fields(db_session, user_id, weak_areas=weak, strong_areas=strong, field_timestamps=ts)
                    adjustments.append(f"'{topic}' moved from weak to strong areas")
                    logger.info("User {}: '{}' moved from weak to strong ({} scores >= {})", user_id, topic, strong_count, tuning.strong_area_score)
                elif weak_count >= tuning.weak_area_min_occurrences and topic not in weak:
                    weak.append(topic)
                    if topic in strong:
                        strong.remove(topic)
                    ts = stamp_field(ts, "weak_areas", weak, date_str)
                    ts = stamp_field(ts, "strong_areas", strong, date_str)
                    await UserRepo.update_fields(db_session, user_id, weak_areas=weak, strong_areas=strong, field_timestamps=ts)
                    adjustments.append(f"'{topic}' added to weak areas")
                    logger.info("User {}: '{}' added to weak areas ({} scores <= {})", user_id, topic, weak_count, tuning.weak_area_score)

                # --- Auto-review vocabulary words used in exercise ---
                # Map exercise score → FSRS rating so exercises count as reviews.
                reviewed_words: list[str] = []
                if words:
                    fsrs_rating = _score_to_fsrs_rating(normalized_score)
                    vocab_rows = await VocabularyRepo.get_by_words_ci(db_session, user_id, words)
                    for vocab in vocab_rows:
                        try:
                            fsrs_result = review_card(vocab, fsrs_rating)
                            await VocabularyRepo.update_fsrs(
                                db_session, vocab.id,
                                fsrs_state=fsrs_result["state"],
                                fsrs_stability=fsrs_result["stability"],
                                fsrs_difficulty=fsrs_result["difficulty"],
                                fsrs_due=fsrs_result["due"],
                                fsrs_last_review=fsrs_result["last_review"],
                                fsrs_data=fsrs_result["card_data"],
                                last_rating=fsrs_rating,
                            )
                            await VocabularyReviewLogRepo.create(
                                db_session,
                                user_id=user_id,
                                vocabulary_id=vocab.id,
                                session_id=_session_uuid(),
                                rating=fsrs_rating,
                            )
                            reviewed_words.append(vocab.word)
                        except Exception:
                            logger.warning("FSRS auto-review failed for word '{}' user {}", vocab.word, user_id)

                await db_session.commit()
            except SQLAlchemyError:
                logger.exception("Failed to record exercise for user {}", user_id)
                return _err("Database error recording exercise, please try again")

        result = {
            "status": "recorded",
            "performance": score_label(normalized_score),
            "topic": topic,
            "recent_trend": score_label(
                sum(scores[-tuning.recent_scores_display:]) / len(scores[-tuning.recent_scores_display:])
            ) if scores else "unknown",
        }
        if adjustments:
            result["adjustments"] = adjustments
        if reviewed_words:
            result["vocabulary_reviewed"] = reviewed_words

        logger.info("Exercise recorded for user {}: {} score={} reviewed_words={}", user_id, topic, score, len(reviewed_words))
        return _ok(result)

    @tool(
        "add_vocabulary",
        "Add a new word to the student's vocabulary. Deduplicates by word.",
        {"word": str, "translation": str, "context_sentence": str, "topic": str},
    )
    async def add_vocabulary(args: dict[str, Any]) -> dict[str, Any]:
        word = args["word"].strip()[:tuning.max_word_length]

        async with session_factory() as db_session:
            # Case-insensitive dedup: "Bonjour" and "bonjour" are the same word
            existing = await VocabularyRepo.get_by_word_ci(db_session, user_id, word)
            if existing:
                return _ok({
                    "status": "duplicate",
                    "word": word,
                    "message": f"'{word}' already in vocabulary",
                })

            # Initialize FSRS card data for proper spaced repetition scheduling
            card_info = create_new_card()

            try:
                vocab = await VocabularyRepo.add(
                    db_session,
                    user_id=user_id,
                    word=word,
                    translation=args.get("translation", "").strip()[:tuning.max_translation_length] or None,
                    context_sentence=args.get("context_sentence", "").strip() or None,
                    topic=args.get("topic", "").strip()[:tuning.max_topic_length] or None,
                    fsrs_state=card_info["state"],
                    fsrs_stability=card_info["stability"],
                    fsrs_difficulty=card_info["difficulty"],
                    fsrs_due=card_info["due"],
                    fsrs_data=card_info["card_data"],
                )

                await db_session.execute(
                    update(User)
                    .where(User.telegram_id == user_id)
                    .values(vocabulary_count=User.vocabulary_count + 1)
                )
                await db_session.commit()
            except IntegrityError:
                # Unique constraint hit — concurrent dedup race (harmless)
                return _ok({
                    "status": "duplicate",
                    "word": word,
                    "message": f"'{word}' already in vocabulary",
                })
            except SQLAlchemyError:
                logger.exception("Failed to add vocabulary for user {}", user_id)
                return _err("Database error adding vocabulary, please try again")

        logger.info("Vocabulary added for user {}: {}", user_id, word)
        return _ok({
            "status": "added",
            "word": word,
            "vocabulary_id": vocab.id,
        })

    @tool(
        "get_due_vocabulary",
        "Fetch vocabulary cards due for FSRS review, sorted by most overdue first. "
        "Optionally filter by topic.",
        {"limit": int, "topic": str},
    )
    async def get_due_vocabulary(args: dict[str, Any]) -> dict[str, Any]:
        limit = min(args.get("limit", tuning.due_vocab_default_limit), tuning.due_vocab_max)
        topic = args.get("topic", "").strip() or None

        async with session_factory() as db_session:
            cards = await VocabularyRepo.get_due(db_session, user_id, limit=limit, topic=topic)
            result = [
                {
                    "id": c.id,
                    "word": c.word,
                    "translation": c.translation,
                    "context_sentence": c.context_sentence,
                    "topic": c.topic,
                    "review_count": c.review_count,
                    "last_rating": c.last_rating,
                    "due_since": _to_local_iso(c.fsrs_due),
                    "last_reviewed": _to_local_iso(c.fsrs_last_review),
                }
                for c in cards
            ]
            return _ok({"due_cards": result, "count": len(result)})

    @tool(
        "manage_schedule",
        "Create, update, delete, or list user schedules. "
        "Action: 'create', 'update', 'delete', 'list'. "
        "For create/update, provide rrule (RFC 5545), schedule_type, description. "
        "For delete, provide schedule_id.",
        {"action": str, "schedule_type": str, "rrule": str, "description": str,
         "schedule_id": str, "notification_tier": str},
    )
    async def manage_schedule(args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"].lower()

        async with session_factory() as db_session:
            if action == "list":
                schedules = await ScheduleRepo.get_for_user(db_session, user_id)
                result = [
                    {
                        "id": str(s.id),
                        "type": s.schedule_type,
                        "description": s.description,
                        "status": s.status,
                        "next_trigger": _to_local_iso(s.next_trigger_at),
                        "rrule": s.rrule,
                    }
                    for s in schedules
                ]
                return _ok({"schedules": result, "count": len(result)})

            if action == "create":
                # Fetch all user schedules once (max 10 rows) for validation
                existing = await ScheduleRepo.get_for_user(db_session, user_id)

                if len(existing) >= tuning.max_schedules_per_user:
                    return _err(f"Maximum {tuning.max_schedules_per_user} schedules per user. Delete one first.")

                rrule_str = args.get("rrule", "")
                if not rrule_str:
                    return _err("rrule is required for creating a schedule")

                schedule_type = args.get("schedule_type", "custom")
                if schedule_type not in ScheduleType:
                    return _err(
                        f"Invalid schedule_type '{schedule_type}'. "
                        f"Use: {', '.join(sorted(ScheduleType))}"
                    )

                notification_tier = args.get("notification_tier", "template")
                if notification_tier not in NotificationTier:
                    return _err(
                        f"Invalid notification_tier '{notification_tier}'. "
                        f"Use: {', '.join(sorted(NotificationTier))}"
                    )

                # Per-type duplicate limit
                type_count = sum(1 for s in existing if s.schedule_type == schedule_type)
                if type_count >= tuning.max_schedules_per_type:
                    return _err(
                        f"Already {type_count} '{schedule_type}' schedules "
                        f"(max {tuning.max_schedules_per_type} per type). "
                        f"Delete one or use a different schedule type."
                    )

                # LLM/hybrid schedule cap — estimate daily LLM notifications from
                # existing schedules' recurrence + the new one.
                if notification_tier in (NotificationTier.LLM, NotificationTier.HYBRID):
                    tier_limits = TIER_LIMITS.get(UserTier(user_tier))
                    if tier_limits:
                        daily_llm_estimate = _estimate_daily_llm_notifications(existing, rrule_str)
                        if daily_llm_estimate > tier_limits.max_llm_notifications_per_day:
                            return _err(
                                f"Too many LLM notifications/day (~{int(daily_llm_estimate)}). "
                                f"Limit is {tier_limits.max_llm_notifications_per_day}/day. "
                                f"Use notification_tier='template' or reduce schedule frequency."
                            )

                # Validate RRULE and compute next trigger in user's local timezone
                user_tz = safe_zoneinfo(user_timezone)
                next_trigger, rrule_error = _validate_and_compute_rrule(rrule_str, user_tz)
                if rrule_error:
                    return _err(rrule_error)

                try:
                    schedule = await ScheduleRepo.create(
                        db_session,
                        user_id=user_id,
                        schedule_type=schedule_type,
                        rrule=rrule_str,
                        next_trigger_at=next_trigger,
                        description=args.get("description", "Custom schedule"),
                        notification_tier=notification_tier,
                        created_by="user",
                    )
                    await db_session.commit()
                except SQLAlchemyError:
                    logger.exception("Failed to create schedule for user {}", user_id)
                    return _err("Database error creating schedule, please try again")

                logger.info("Schedule created for user {}: {}", user_id, schedule.id)
                return _ok({
                    "status": "created",
                    "schedule_id": str(schedule.id),
                    "next_trigger": _to_local_iso(next_trigger),
                })

            if action == "update":
                schedule_id_str = args.get("schedule_id")
                if not schedule_id_str:
                    return _err("schedule_id is required for update")

                try:
                    sid = _uuid.UUID(schedule_id_str)
                except ValueError:
                    return _err("Invalid schedule_id format")

                schedule = await ScheduleRepo.get(db_session, sid)
                if schedule is None or schedule.user_id != user_id:
                    return _err("Schedule not found")

                updates: dict[str, Any] = {}
                if args.get("rrule"):
                    upd_tz = safe_zoneinfo(user_timezone)
                    upd_next, rrule_error = _validate_and_compute_rrule(args["rrule"], upd_tz)
                    if rrule_error:
                        return _err(rrule_error)
                    updates["rrule"] = args["rrule"]
                    updates["next_trigger_at"] = upd_next

                if args.get("description"):
                    updates["description"] = args["description"]
                if args.get("notification_tier"):
                    if args["notification_tier"] not in NotificationTier:
                        return _err(
                            f"Invalid notification_tier '{args['notification_tier']}'. "
                            f"Use: {', '.join(sorted(NotificationTier))}"
                        )
                    # LLM/hybrid cap when upgrading from template
                    new_tier = args["notification_tier"]
                    if (
                        new_tier in (NotificationTier.LLM, NotificationTier.HYBRID)
                        and schedule.notification_tier not in (NotificationTier.LLM, NotificationTier.HYBRID)
                    ):
                        tier_limits = TIER_LIMITS.get(UserTier(user_tier))
                        if tier_limits:
                            all_schedules = await ScheduleRepo.get_for_user(db_session, user_id)
                            daily_estimate = _estimate_daily_llm_notifications(all_schedules)
                            if daily_estimate >= tier_limits.max_llm_notifications_per_day:
                                return _err(
                                    f"LLM schedule limit reached ({tier_limits.max_llm_notifications_per_day}). "
                                    f"Delete an existing LLM schedule or keep template tier."
                                )
                    updates["notification_tier"] = new_tier
                if args.get("schedule_type"):
                    if args["schedule_type"] not in ScheduleType:
                        return _err(
                            f"Invalid schedule_type '{args['schedule_type']}'. "
                            f"Use: {', '.join(sorted(ScheduleType))}"
                        )
                    updates["schedule_type"] = args["schedule_type"]

                if updates:
                    try:
                        await ScheduleRepo.update_fields(db_session, sid, **updates)
                        await db_session.commit()
                    except SQLAlchemyError:
                        logger.exception("Failed to update schedule {}", schedule_id_str)
                        return _err("Database error updating schedule, please try again")

                return _ok({"status": "updated", "schedule_id": schedule_id_str})

            if action == "delete":
                schedule_id_str = args.get("schedule_id")
                if not schedule_id_str:
                    return _err("schedule_id is required for delete")

                try:
                    sid = _uuid.UUID(schedule_id_str)
                except ValueError:
                    return _err("Invalid schedule_id format")

                schedule = await ScheduleRepo.get(db_session, sid)
                if schedule is None or schedule.user_id != user_id:
                    return _err("Schedule not found")

                try:
                    await ScheduleRepo.delete(db_session, sid)
                    await db_session.commit()
                except SQLAlchemyError:
                    logger.exception("Failed to delete schedule {}", schedule_id_str)
                    return _err("Database error deleting schedule, please try again")
                logger.info("Schedule deleted for user {}: {}", user_id, schedule_id_str)
                return _ok({"status": "deleted", "schedule_id": schedule_id_str})

            return _err(f"Unknown action '{action}'. Use: create, update, delete, list")

    @tool(
        "search_vocabulary",
        "Search the student's existing vocabulary by keyword or topic. "
        f"Case-insensitive, returns up to {tuning.vocab_search_limit} results.",
        {"query": str},
    )
    async def search_vocabulary(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query:
            return _err("Search query cannot be empty")

        async with session_factory() as db_session:
            results = await VocabularyRepo.search(db_session, user_id, query, limit=tuning.vocab_search_limit)
            cards = [
                {
                    "id": v.id,
                    "word": v.word,
                    "translation": v.translation,
                    "topic": v.topic,
                    "review_count": v.review_count,
                    "last_rating": v.last_rating,
                }
                for v in results
            ]
            return _ok({"results": cards, "count": len(cards)})

    @tool(
        "get_exercise_history",
        "Get recent exercise results, optionally filtered by topic. "
        "Useful for tracking performance trends.",
        {"limit": int, "topic": str},
    )
    async def get_exercise_history(args: dict[str, Any]) -> dict[str, Any]:
        limit = min(args.get("limit", tuning.exercise_history_default_limit), tuning.exercise_history_max)
        topic = args.get("topic", "").strip() or None

        async with session_factory() as db_session:
            results = await ExerciseResultRepo.get_recent(
                db_session, user_id, limit=limit, topic=topic,
            )
            items = [
                {
                    "exercise_type": r.exercise_type,
                    "topic": r.topic,
                    "score": r.score,
                    "max_score": r.max_score,
                    "words_involved": r.words_involved,
                    "notes": r.agent_notes,
                    "date": _to_local_iso(r.created_at),
                }
                for r in results
            ]
            return _ok({"history": items, "count": len(items)})

    @tool(
        "get_progress_summary",
        "Get a comprehensive progress summary with score trends (7-day and 30-day), "
        "per-topic performance breakdown, vocabulary learning stats (new/learning/"
        "review/relearning), and session activity. Use this to understand long-term "
        "learning patterns, identify improvement areas, and give informed feedback.",
        {"days": int},
    )
    async def get_progress_summary(args: dict[str, Any]) -> dict[str, Any]:
        days = min(max(args.get("days", tuning.progress_summary_default_days), 1), tuning.progress_summary_max_days)

        async with session_factory() as db_session:
            try:
                score_7d = await ExerciseResultRepo.get_score_summary(
                    db_session, user_id, days=7,
                )
                score_period = await ExerciseResultRepo.get_score_summary(
                    db_session, user_id, days=days,
                )
                score_alltime = await ExerciseResultRepo.get_score_summary(
                    db_session, user_id, days=None,
                )
                topic_stats = await ExerciseResultRepo.get_topic_stats(
                    db_session, user_id, days=days,
                )
                vocab_states = await VocabularyRepo.get_state_counts(db_session, user_id)
                due_count = await VocabularyRepo.count_due(db_session, user_id)
                sessions_week = await SessionRepo.get_activity_stats(
                    db_session, user_id, days=7,
                )
                sessions_period = await SessionRepo.get_activity_stats(
                    db_session, user_id, days=days,
                )
            except SQLAlchemyError:
                logger.exception("Failed to get progress summary for user {}", user_id)
                return _err("Database error fetching progress summary, please try again")

            total_vocab = sum(vocab_states.values())

            result = {
                "score_trends": {
                    "last_7_days": score_7d,
                    "last_{}_days".format(days): score_period,
                    "all_time": score_alltime,
                },
                "topic_performance": topic_stats,
                "vocabulary": {
                    "total": total_vocab,
                    "due_for_review": due_count,
                    # FSRS states: 0=New, 1=Learning, 2=Review, 3=Relearning
                    "new": vocab_states.get(0, 0),
                    "learning": vocab_states.get(1, 0),
                    "review": vocab_states.get(2, 0),
                    "relearning": vocab_states.get(3, 0),
                },
                "sessions": {
                    "this_week": sessions_week["session_count"],
                    "this_week_avg_duration_min": (
                        round(sessions_week["avg_duration_ms"] / 60000, 1)
                        if sessions_week["avg_duration_ms"] else None
                    ),
                    "last_{}_days".format(days): sessions_period["session_count"],
                },
            }

            return _ok(result)

    # ---------------------------------------------------------------
    # manage_learning_plan
    # ---------------------------------------------------------------

    @tool(
        "manage_learning_plan",
        "Manage the student's learning plan. "
        "Actions: 'get' (retrieve active plan with derived progress), "
        "'create' (create a new plan — supersedes any existing active plan), "
        "'adapt' (modify remaining phases when student is ahead/behind). "
        "Plans have a 2-8 week horizon and target the next CEFR level. "
        "When recording exercises for plan topics, use the exact plan topic names in the topic field.",
        {"action": str, "description": str, "total_weeks": int,
         "phases": str, "updated_phases": str, "adaptation_reason": str},
    )
    async def manage_learning_plan(args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"].lower()

        # Proactive sessions can only read
        _read_only = session_type not in (
            SessionType.INTERACTIVE, SessionType.ONBOARDING,
        )

        async with session_factory() as db_session:
            try:
                if action == "get":
                    plan = await LearningPlanRepo.get_active(db_session, user_id)
                    if plan is None:
                        return _ok({"has_plan": False})

                    # Derive progress from ExerciseResult data
                    topic_stats = await fetch_plan_topic_stats(
                        db_session, user_id, plan,
                    )
                    progress = compute_plan_progress(
                        plan.plan_data or {},
                        plan.total_weeks,
                        plan.start_date,
                        topic_stats,
                    )

                    # Auto-extend with consolidation phase if 100% but level not reached
                    if progress["progress_pct"] == 100:
                        user = await UserRepo.get(db_session, user_id)
                        if user and plan.current_level == plan.target_level:
                            # Mastery plan (e.g. C2→C2): auto-complete at 100%
                            await _record_plan_completion(
                                db_session, user_id, plan.current_level, plan.target_level,
                            )
                            await LearningPlanRepo.delete(db_session, user_id)
                            await db_session.commit()
                            return _ok({
                                "has_plan": False,
                                "plan_completed": True,
                                "message": (
                                    f"Mastery plan completed! All {plan.target_level}-level "
                                    f"topics covered. The student can create a new plan "
                                    f"to explore different advanced topics."
                                ),
                            })
                        elif user and await _maybe_add_consolidation_phase(
                            db_session, plan, progress, topic_stats, user.level,
                        ):
                            # Re-fetch updated plan and recompute
                            plan = await LearningPlanRepo.get_active(db_session, user_id)
                            topic_stats = await fetch_plan_topic_stats(
                                db_session, user_id, plan,
                            )
                            progress = compute_plan_progress(
                                plan.plan_data or {},
                                plan.total_weeks,
                                plan.start_date,
                                topic_stats,
                            )

                    # Compute current week from date
                    today = datetime.now(_user_tz).date()
                    elapsed_days = (today - plan.start_date).days
                    current_week = max(1, min(plan.total_weeks, elapsed_days // 7 + 1))
                    days_remaining = max(0, (plan.target_end_date - today).days)

                    # Replace raw avg_score with qualitative label (Rule #7)
                    for phase in progress["phases"]:
                        for topic in phase.get("topics", []):
                            if "avg_score" in topic:
                                topic["avg_score"] = score_label(topic["avg_score"])

                    return _ok({
                        "has_plan": True,
                        "plan": {
                            "id": str(plan.id),
                            "current_level": plan.current_level,
                            "target_level": plan.target_level,
                            "start_date": plan.start_date.isoformat(),
                            "target_end_date": plan.target_end_date.isoformat(),
                            "days_remaining": days_remaining,
                            "current_week": current_week,
                            "total_weeks": plan.total_weeks,
                            "progress_pct": progress["progress_pct"],
                            "completed_topics": progress["completed_topics"],
                            "total_topics": progress["total_topics"],
                            "times_adapted": plan.times_adapted,
                            "description": (plan.plan_data or {}).get("description", ""),
                            "phases": progress["phases"],
                        },
                    })

                if _read_only:
                    return _err("This session type can only read learning plans (action='get').")

                if action == "create":
                    desc = str(args.get("description", "")).strip()
                    if not desc:
                        return _err("description is required")

                    total_weeks = int(args.get("total_weeks", tuning.plan_default_weeks))
                    if not (tuning.plan_min_weeks <= total_weeks <= tuning.plan_max_weeks):
                        return _err(
                            f"total_weeks must be between {tuning.plan_min_weeks} "
                            f"and {tuning.plan_max_weeks}"
                        )

                    # Parse phases JSON
                    phases_raw = args.get("phases", "[]")
                    try:
                        phases = json.loads(phases_raw) if isinstance(phases_raw, str) else phases_raw
                    except json.JSONDecodeError:
                        return _err("phases must be valid JSON array")
                    if not isinstance(phases, list) or len(phases) != total_weeks:
                        return _err(f"phases must be a JSON array with exactly {total_weeks} entries")

                    # Validate each phase
                    for i, phase in enumerate(phases):
                        if not isinstance(phase, dict):
                            return _err(f"Phase {i + 1} must be a JSON object")
                        if not phase.get("focus"):
                            return _err(f"Phase {i + 1} missing 'focus'")
                        topics = phase.get("topics", [])
                        if not isinstance(topics, list) or not topics:
                            return _err(f"Phase {i + 1} must have at least one topic")
                        if len(topics) > tuning.plan_max_topics_per_week:
                            return _err(
                                f"Phase {i + 1} has {len(topics)} topics, "
                                f"max is {tuning.plan_max_topics_per_week}"
                            )

                    # Compute dates
                    today = datetime.now(_user_tz).date()
                    target_end = today + timedelta(weeks=total_weeks)

                    # Build plan_data with computed dates per phase
                    enriched_phases = []
                    for i, phase in enumerate(phases):
                        phase_start = today + timedelta(weeks=i)
                        phase_end = today + timedelta(weeks=i + 1) - timedelta(days=1)
                        enriched = {
                            "week": i + 1,
                            "start_date": phase_start.isoformat(),
                            "end_date": phase_end.isoformat(),
                            "focus": str(phase["focus"]).strip()[:tuning.plan_max_focus_length],
                            "topics": [str(t).strip()[:tuning.plan_max_topic_length] for t in phase["topics"]],
                        }
                        if phase.get("vocabulary_theme"):
                            enriched["vocabulary_theme"] = str(phase["vocabulary_theme"]).strip()[:tuning.plan_max_vocab_theme_length]
                        if phase.get("vocabulary_target"):
                            enriched["vocabulary_target"] = min(
                                int(phase["vocabulary_target"]), tuning.plan_max_vocab_target_per_week,
                            )
                        if phase.get("assessment"):
                            enriched["assessment"] = phase["assessment"]
                        enriched_phases.append(enriched)

                    # Determine target level
                    user = await UserRepo.get(db_session, user_id)
                    if user is None:
                        return _err("User not found")
                    current_level = user.level
                    current_idx = CEFR_LEVELS.index(current_level) if current_level in CEFR_LEVELS else 0
                    target_level = (
                        CEFR_LEVELS[current_idx + 1]
                        if current_idx < len(CEFR_LEVELS) - 1
                        else current_level
                    )

                    plan_data = {
                        "description": desc[:tuning.plan_max_description_length],
                        "weekly_sessions_target": int(args.get("weekly_sessions_target", tuning.plan_default_weekly_sessions)),
                        "phases": enriched_phases,
                        "adaptation_log": [],
                    }

                    # Record completion of existing plan before superseding
                    old_plan = await LearningPlanRepo.get_active(db_session, user_id)
                    if old_plan:
                        await _record_plan_completion(
                            db_session, user_id,
                            old_plan.current_level, old_plan.target_level,
                        )

                    # create() deletes any existing plan for this user
                    new_plan = await LearningPlanRepo.create(
                        db_session,
                        user_id=user_id,
                        current_level=current_level,
                        target_level=target_level,
                        start_date=today,
                        target_end_date=target_end,
                        total_weeks=total_weeks,
                        plan_data=plan_data,
                    )
                    await db_session.commit()

                    return _ok({
                        "status": "created",
                        "plan_id": str(new_plan.id),
                        "current_level": current_level,
                        "target_level": target_level,
                        "start_date": today.isoformat(),
                        "target_end_date": target_end.isoformat(),
                        "total_weeks": total_weeks,
                        "total_topics": sum(len(p.get("topics", [])) for p in enriched_phases),
                    })

                if action == "adapt":
                    plan = await LearningPlanRepo.get_active(db_session, user_id)
                    if plan is None:
                        return _err("No active learning plan to adapt")

                    reason = str(args.get("adaptation_reason", "")).strip()
                    if not reason:
                        return _err("adaptation_reason is required")

                    updated_raw = args.get("updated_phases", "[]")
                    try:
                        updated_phases = (
                            json.loads(updated_raw) if isinstance(updated_raw, str) else updated_raw
                        )
                    except json.JSONDecodeError:
                        return _err("updated_phases must be valid JSON array")
                    if not isinstance(updated_phases, list) or not updated_phases:
                        return _err("updated_phases must be a non-empty JSON array")

                    # Compute current week
                    today = datetime.now(_user_tz).date()
                    elapsed_days = (today - plan.start_date).days
                    current_week = max(1, min(plan.total_weeks, elapsed_days // 7 + 1))

                    # Validate phase structure
                    for i, phase in enumerate(updated_phases):
                        if not isinstance(phase, dict):
                            return _err(f"Updated phase {i + 1} must be a JSON object")
                        if not phase.get("focus"):
                            return _err(f"Updated phase {i + 1} missing 'focus'")
                        topics = phase.get("topics", [])
                        if not isinstance(topics, list) or not topics:
                            return _err(f"Updated phase {i + 1} must have at least one topic")
                        if len(topics) > tuning.plan_max_topics_per_week:
                            return _err(
                                f"Updated phase {i + 1} has {len(topics)} topics, "
                                f"max is {tuning.plan_max_topics_per_week}"
                            )

                    # Replace future phases, keep past/current ones
                    plan_data = dict(plan.plan_data or {})
                    old_phases = plan_data.get("phases", [])
                    kept_phases = [p for p in old_phases if p.get("week", 0) <= current_week]

                    # Enrich updated phases with dates
                    new_total_weeks = current_week + len(updated_phases)
                    if new_total_weeks > tuning.plan_max_weeks:
                        return _err(
                            f"Adapted plan would be {new_total_weeks} weeks, "
                            f"but maximum is {tuning.plan_max_weeks}. "
                            f"Reduce the number of phases (currently {len(updated_phases)}) "
                            f"or combine topics."
                        )
                    new_end_date = plan.start_date + timedelta(weeks=new_total_weeks)

                    for i, phase in enumerate(updated_phases):
                        week_num = current_week + i + 1
                        phase_start = plan.start_date + timedelta(weeks=week_num - 1)
                        phase_end = plan.start_date + timedelta(weeks=week_num) - timedelta(days=1)
                        enriched = {
                            "week": week_num,
                            "start_date": phase_start.isoformat(),
                            "end_date": phase_end.isoformat(),
                            "focus": str(phase["focus"]).strip()[:tuning.plan_max_focus_length],
                            "topics": [str(t).strip()[:tuning.plan_max_topic_length] for t in phase["topics"]],
                        }
                        if phase.get("vocabulary_theme"):
                            enriched["vocabulary_theme"] = str(phase["vocabulary_theme"]).strip()[:tuning.plan_max_vocab_theme_length]
                        if phase.get("vocabulary_target"):
                            enriched["vocabulary_target"] = min(int(phase["vocabulary_target"]), tuning.plan_max_vocab_target_per_week)
                        if phase.get("assessment"):
                            enriched["assessment"] = phase["assessment"]
                        kept_phases.append(enriched)

                    plan_data["phases"] = kept_phases

                    # Record adaptation
                    log = plan_data.setdefault("adaptation_log", [])
                    log.append({
                        "date": today.isoformat(),
                        "reason": reason[:tuning.plan_max_adaptation_reason_length],
                        "week": current_week,
                    })

                    await LearningPlanRepo.update_fields(
                        db_session,
                        plan.id,
                        plan_data=plan_data,
                        total_weeks=new_total_weeks,
                        target_end_date=new_end_date,
                        times_adapted=plan.times_adapted + 1,
                        last_adapted_at=datetime.now(timezone.utc),
                    )
                    await db_session.commit()

                    return _ok({
                        "status": "adapted",
                        "plan_id": str(plan.id),
                        "new_total_weeks": new_total_weeks,
                        "new_end_date": new_end_date.isoformat(),
                        "times_adapted": plan.times_adapted + 1,
                        "reason": reason,
                    })

                return _err(f"Unknown action '{action}'. Use: get, create, adapt")

            except (IntegrityError, SQLAlchemyError):
                logger.exception("Failed to manage learning plan for user {}", user_id)
                return _err("Database error managing learning plan, please try again")

    # --- Web search tool (conditional on API key) ---

    web_search_tool = None
    web_extract_tool = None
    if web_search_available() and user_tier == UserTier.PREMIUM:
        _search_count = 0

        @tool(
            "web_search",
            "Search the web for real-world content: news articles, cultural material, "
            "authentic usage examples in the target language, or reading comprehension "
            "material. Returns search results with titles, URLs, and content extracts. "
            "Do NOT use for translations or grammar rules — use your own knowledge.",
            {
                "query": str,
                "max_results": int,
                "topic": str,
            },
        )
        async def web_search(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal _search_count

            if _search_count >= tuning.max_searches_per_session:
                return _err(
                    f"Search limit reached ({tuning.max_searches_per_session} per session). "
                    "Use your own knowledge for the rest of this session."
                )

            query = str(args.get("query", "")).strip()
            if not query:
                return _err("Query cannot be empty")
            query = query[:tuning.web_search_max_query_length]

            max_results = min(
                int(args.get("max_results", tuning.web_search_max_results)),
                tuning.web_search_max_results,
            )
            topic = str(args.get("topic", "general"))
            if topic not in ("general", "news"):
                topic = "general"

            try:
                client = AsyncTavilyClient(api_key=settings.tavily_api_key)
                raw = await asyncio.wait_for(
                    client.search(
                        query=query,
                        max_results=max_results,
                        topic=topic,
                        include_answer=True,
                    ),
                    timeout=tuning.web_search_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return _err("Web search timed out, please try a shorter or simpler query")
            except Exception as e:
                logger.warning("Web search failed for user {}: {}", user_id, e)
                return _err("Web search temporarily unavailable, please continue without it")

            _search_count += 1

            results = []
            for r in raw.get("results", [])[:max_results]:
                entry: dict[str, Any] = {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:1000],
                }
                if r.get("published_date"):
                    entry["published_date"] = r["published_date"]
                results.append(entry)

            return _ok({
                "answer": raw.get("answer") or "",
                "results": results,
                "searches_remaining": tuning.max_searches_per_session - _search_count,
            })

        web_search_tool = web_search

        @tool(
            "web_extract",
            "Extract full content from a specific web page URL. Use this after web_search "
            "to get the complete article text for reading comprehension exercises, "
            "vocabulary extraction, or discussion material. Returns the page title and "
            "content as clean text. Shares the per-session search limit with web_search.",
            {
                "url": str,
            },
        )
        async def web_extract(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal _search_count

            if _search_count >= tuning.max_searches_per_session:
                return _err(
                    f"Search limit reached ({tuning.max_searches_per_session} per session). "
                    "Use your own knowledge for the rest of this session."
                )

            url = str(args.get("url", "")).strip()
            if not url:
                return _err("URL cannot be empty")
            if not url.startswith(("http://", "https://")):
                return _err("URL must start with http:// or https://")

            try:
                client = AsyncTavilyClient(api_key=settings.tavily_api_key)
                raw = await asyncio.wait_for(
                    client.extract(urls=[url]),
                    timeout=tuning.web_search_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return _err("Page extraction timed out, try a different URL")
            except Exception as e:
                logger.warning("Web extract failed for user {}: {}", user_id, e)
                return _err("Page extraction temporarily unavailable")

            _search_count += 1

            results = raw.get("results", [])
            failed = raw.get("failed_results", [])

            if not results:
                reason = failed[0].get("error", "unknown error") if failed else "unknown error"
                return _err(f"Could not extract content from URL: {reason}")

            page = results[0]
            content = page.get("raw_content", "")
            # Truncate to configured limit
            if len(content) > tuning.web_extract_max_content_chars:
                content = content[:tuning.web_extract_max_content_chars] + "\n\n[Content truncated]"

            return _ok({
                "title": page.get("title", ""),
                "url": page.get("url", url),
                "content": content,
                "searches_remaining": tuning.max_searches_per_session - _search_count,
            })

        web_extract_tool = web_extract

    # --- end_session tool ---

    @tool(
        "end_session",
        "End the current session. Call this when you believe the lesson is complete "
        "(e.g. after 3+ exercises, student says goodbye, or natural conclusion). "
        "After calling this tool, give a brief warm farewell message. "
        "The session will close automatically and the student will receive a summary.",
        {},
    )
    async def end_session(args: dict[str, Any]) -> dict[str, Any]:
        """Signal that the session should end."""
        return _ok({"status": "session_end_requested"})

    # ---------------------------------------------------------------
    # adjust_level
    # ---------------------------------------------------------------

    @tool(
        "adjust_level",
        "Adjust the student's CEFR level after assessment. "
        "Call ONLY after conducting a thorough assessment of the student's abilities "
        f"(at least {tuning.level_adjust_min_assessment_exercises} scored exercises in this session). "
        "Provide a clear justification based on observed performance.",
        {"new_level": str, "justification": str},
    )
    async def adjust_level(args: dict[str, Any]) -> dict[str, Any]:
        new_level = str(args.get("new_level", "")).strip().upper()
        justification = str(args.get("justification", "")).strip()

        if new_level not in CEFR_LEVELS:
            return _err(f"Invalid level '{new_level}'. Must be one of: {', '.join(CEFR_LEVELS)}")
        if not justification or len(justification) < 20:
            return _err("Provide a detailed justification (at least 20 characters)")

        async with session_factory() as db_session:
            try:
                user = await UserRepo.get(db_session, user_id)
                if user is None:
                    return _err("User not found")
                if user.level == new_level:
                    return _err(f"Student is already at level {new_level}")

                old_idx = CEFR_LEVELS.index(user.level) if user.level in CEFR_LEVELS else 0
                new_idx = CEFR_LEVELS.index(new_level)
                if abs(new_idx - old_idx) > 1:
                    return _err("Can only adjust by one CEFR level at a time")

                ts = dict(user.field_timestamps or {})
                date_str = datetime.now(_user_tz).strftime("%Y-%m-%d")
                ts = stamp_field(ts, "level", new_level, date_str)

                await UserRepo.update_fields(
                    db_session, user_id, level=new_level, field_timestamps=ts,
                )

                # Handle learning plan impact
                plan_note = ""
                active_plan = await LearningPlanRepo.get_active(db_session, user_id)
                if active_plan:
                    target_idx = (
                        CEFR_LEVELS.index(active_plan.target_level)
                        if active_plan.target_level in CEFR_LEVELS else 0
                    )
                    if new_idx >= target_idx:
                        await _record_plan_completion(
                            db_session, user_id,
                            active_plan.current_level, active_plan.target_level,
                        )
                        await LearningPlanRepo.delete(db_session, user_id)
                        plan_note = (
                            f"Learning plan completed — reached target level "
                            f"{active_plan.target_level}. Create a new plan for further progression."
                        )

                await db_session.commit()
            except SQLAlchemyError:
                logger.exception("Failed to adjust level for user {}", user_id)
                return _err("Database error adjusting level, please try again")

        old_level = user.level
        direction = "UP" if new_idx > old_idx else "DOWN"
        result: dict[str, Any] = {
            "status": "level_adjusted",
            "direction": direction,
            "old_level": old_level,
            "new_level": new_level,
            "justification": justification,
        }
        if plan_note:
            result["plan_impact"] = plan_note

        logger.info("Level adjusted for user {}: {} → {} ({})", user_id, old_level, new_level, direction)
        return _ok(result)

    # --- Build list and permission check ---

    all_tools = [
        get_user_profile,
        update_preference,
        record_exercise_result,
        add_vocabulary,
        get_due_vocabulary,
        manage_schedule,
        search_vocabulary,
        get_exercise_history,
        get_progress_summary,
        manage_learning_plan,
        adjust_level,
        end_session,
    ]
    if web_search_tool is not None:
        all_tools.append(web_search_tool)
        all_tools.append(web_extract_tool)

    def can_use_tool(tool_name: str) -> bool:
        bare_name = strip_mcp_prefix(tool_name)
        allowed = _SESSION_TYPE_TOOLS.get(session_type, set())
        return bare_name in allowed

    return all_tools, can_use_tool


def create_langbot_server(tools: list) -> Any:
    """Create the in-process MCP server with the given tools."""
    return create_sdk_mcp_server(
        name="langbot",
        version="1.0.0",
        tools=tools,
    )
