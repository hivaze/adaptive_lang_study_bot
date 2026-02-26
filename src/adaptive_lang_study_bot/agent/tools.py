import json
import re
import uuid as _uuid
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from loguru import logger
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from dateutil.rrule import DAILY, HOURLY, MINUTELY, MONTHLY, SECONDLY, WEEKLY, YEARLY, rrulestr

from adaptive_lang_study_bot.config import TIER_LIMITS, tuning
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
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
from adaptive_lang_study_bot.utils import compute_next_trigger, safe_zoneinfo, strip_mcp_prefix

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_MUTABLE_FIELDS = {"interests", "learning_goals", "preferred_difficulty", "session_style", "topics_to_avoid", "notifications_paused", "additional_notes"}
_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
_MAX_SCHEDULES_PER_USER = 10
_MAX_SCHEDULES_PER_TYPE = 3
_VOCAB_SEARCH_LIMIT = 20
_EXERCISE_HISTORY_MAX = 50
_DUE_VOCAB_MAX = 50

# Base period in minutes for each dateutil frequency constant.
# Uses private _freq / _interval attrs — stable across all dateutil versions.
_FREQ_BASE_MINUTES: dict[int, float] = {
    YEARLY: 525960, MONTHLY: 43200, WEEKLY: 10080,
    DAILY: 1440, HOURLY: 60, MINUTELY: 1, SECONDLY: 1 / 60,
}


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
    "mcp__langbot__send_notification",
    "mcp__langbot__search_vocabulary",
    "mcp__langbot__get_exercise_history",
    "mcp__langbot__get_progress_summary",
]

# Tools allowed per session type
_SESSION_TYPE_TOOLS: dict[SessionType, set[str]] = {
    SessionType.INTERACTIVE: {
        "get_user_profile", "update_preference", "record_exercise_result",
        "add_vocabulary", "get_due_vocabulary",
        "manage_schedule", "search_vocabulary", "get_exercise_history",
        "get_progress_summary",
    },
    SessionType.ONBOARDING: {
        "get_user_profile", "update_preference", "record_exercise_result",
        "add_vocabulary", "search_vocabulary", "manage_schedule",
    },
    SessionType.PROACTIVE_REVIEW: {
        "get_user_profile", "get_due_vocabulary",
        "send_notification",
    },
    SessionType.PROACTIVE_QUIZ: {
        "get_user_profile", "record_exercise_result", "send_notification",
    },
    SessionType.PROACTIVE_SUMMARY: {
        "get_user_profile", "get_exercise_history", "get_progress_summary",
        "send_notification",
    },
    SessionType.PROACTIVE_NUDGE: {
        "get_user_profile", "send_notification",
    },
    SessionType.ASSESSMENT: set(),
}


# Telegram-supported HTML tags (https://core.telegram.org/bots/api#html-style)
_TELEGRAM_HTML_TAGS = frozenset({
    "b", "i", "u", "s", "code", "pre", "a",
    "tg-spoiler", "blockquote", "tg-emoji",
})


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


def _validate_telegram_html(text: str) -> str | None:
    """Check for unbalanced Telegram HTML tags. Returns error message or None."""
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z][a-z0-9-]*)(?:\s[^>]*)?\s*/?>", text, re.IGNORECASE):
        is_close, tag = m.group(1) == "/", m.group(2).lower()
        if tag not in _TELEGRAM_HTML_TAGS:
            continue  # ignore non-Telegram tags (may be literal text)
        if is_close:
            if not stack or stack[-1] != tag:
                return f"Unbalanced HTML: unexpected </{tag}>"
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        unclosed = ", ".join(f"<{tag}>" for tag in stack)
        return f"Unclosed HTML tags: {unclosed}. Close all tags properly."
    return None


# ---------------------------------------------------------------------------
# Per-session tool factory
# ---------------------------------------------------------------------------

def create_session_tools(
    session_factory: Callable,
    user_id: int,
    session_id: str | None = None,
    session_type: SessionType = SessionType.INTERACTIVE,
    user_timezone: str = "UTC",
    notification_sink: list[str] | None = None,
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
        "'Prepare for DELF B2 exam', 'Learn cooking vocabulary for trip to France'.",
        {"field": str, "value": str},
    )
    async def update_preference(args: dict[str, Any]) -> dict[str, Any]:
        field = args["field"]
        raw_value = args["value"]

        if field not in _USER_MUTABLE_FIELDS:
            return _err(
                f"Cannot update '{field}'. Allowed: {', '.join(sorted(_USER_MUTABLE_FIELDS))}",
            )

        if field == "interests":
            value = _parse_list_field(raw_value, max_items=tuning.max_interests, max_len=100)
        elif field == "topics_to_avoid":
            value = _parse_list_field(raw_value, max_items=tuning.max_topics_to_avoid, max_len=100)
        elif field == "learning_goals":
            value = _parse_list_field(raw_value, max_items=tuning.max_learning_goals, max_len=200)
        elif field == "preferred_difficulty":
            value = raw_value.lower().strip()
            if value not in Difficulty:
                return _err(f"Invalid difficulty '{value}'. Use: {', '.join(Difficulty)}")
        elif field == "session_style":
            value = raw_value.lower().strip()
            if value not in SessionStyle:
                return _err(f"Invalid style '{value}'. Use: {', '.join(SessionStyle)}")
        elif field == "additional_notes":
            value = _parse_list_field(raw_value, max_items=tuning.max_additional_notes, max_len=200)
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
                await UserRepo.update_fields(db_session, user_id, **{field: value})
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
        "Auto-adjusts level and weak/strong areas based on scores. "
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
        exercise_type = str(args["exercise_type"]).strip()[:50]
        topic = str(args["topic"]).strip()[:100]

        words_raw = args.get("words_involved", "[]")
        try:
            words = json.loads(words_raw) if isinstance(words_raw, str) else words_raw
        except json.JSONDecodeError:
            words = [words_raw] if words_raw else []
        if not isinstance(words, list):
            words = [words]
        words = [str(w).strip()[:100] for w in words[:20]]

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

                # Level adjustment based on recent scores
                window = tuning.level_recent_window
                if len(scores) >= window:
                    recent_n = scores[-window:]
                    avg = sum(recent_n) / len(recent_n)
                    current_idx = _LEVELS.index(user.level) if user.level in _LEVELS else 0

                    if avg >= tuning.level_up_avg and current_idx < len(_LEVELS) - 1:
                        new_level = _LEVELS[current_idx + 1]
                        await UserRepo.update_fields(db_session, user_id, level=new_level)
                        adjustments.append(f"Level UP: {user.level} → {new_level}")
                    elif avg <= tuning.level_down_avg and current_idx > 0:
                        new_level = _LEVELS[current_idx - 1]
                        await UserRepo.update_fields(db_session, user_id, level=new_level)
                        adjustments.append(f"Level DOWN: {user.level} → {new_level}")

                # Weak/strong area adjustment — requires multiple qualifying
                # scores for the same topic before modifying areas.
                weak = list(user.weak_areas or [])
                strong = list(user.strong_areas or [])

                recent_topic_results = await ExerciseResultRepo.get_recent(
                    db_session, user_id, topic=topic, limit=5,
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
                    strong = strong[-10:]
                    await UserRepo.update_fields(db_session, user_id, weak_areas=weak, strong_areas=strong)
                    adjustments.append(f"'{topic}' moved from weak to strong areas")
                    logger.info("User {}: '{}' moved from weak to strong ({} scores >= {})", user_id, topic, strong_count, tuning.strong_area_score)
                elif weak_count >= tuning.weak_area_min_occurrences and topic not in weak:
                    weak.append(topic)
                    weak = weak[-10:]
                    if topic in strong:
                        strong.remove(topic)
                    await UserRepo.update_fields(db_session, user_id, weak_areas=weak, strong_areas=strong)
                    adjustments.append(f"'{topic}' added to weak areas")
                    logger.info("User {}: '{}' added to weak areas ({} scores <= {})", user_id, topic, weak_count, tuning.weak_area_score)

                # --- Auto-review vocabulary words used in exercise ---
                # Map exercise score → FSRS rating so exercises count as reviews.
                reviewed_words: list[str] = []
                if words:
                    fsrs_rating = (
                        1 if normalized_score <= 3
                        else 2 if normalized_score <= 5
                        else 4 if normalized_score >= 9
                        else 3
                    )
                    for w in words:
                        vocab = await VocabularyRepo.get_by_word_ci(db_session, user_id, w)
                        if vocab is None:
                            continue
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
                            logger.warning("FSRS auto-review failed for word '{}' user {}", w, user_id)

                await db_session.commit()
            except SQLAlchemyError:
                logger.exception("Failed to record exercise for user {}", user_id)
                return _err("Database error recording exercise, please try again")

        result = {
            "status": "recorded",
            "score": score,
            "topic": topic,
            "recent_scores": scores[-5:],
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
        word = args["word"].strip()[:200]

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
                    translation=args.get("translation", "").strip()[:200] or None,
                    context_sentence=args.get("context_sentence", "").strip() or None,
                    topic=args.get("topic", "").strip()[:100] or None,
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
        limit = min(args.get("limit", 20), _DUE_VOCAB_MAX)
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

                if len(existing) >= _MAX_SCHEDULES_PER_USER:
                    return _err(f"Maximum {_MAX_SCHEDULES_PER_USER} schedules per user. Delete one first.")

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
                if type_count >= _MAX_SCHEDULES_PER_TYPE:
                    return _err(
                        f"Already {type_count} '{schedule_type}' schedules "
                        f"(max {_MAX_SCHEDULES_PER_TYPE} per type). "
                        f"Delete one or use a different schedule type."
                    )

                # LLM/hybrid schedule cap — estimate daily LLM notifications from
                # existing schedules' recurrence + the new one. Prevents creating
                # hourly LLM schedules that would blow the daily budget.
                if notification_tier in (NotificationTier.LLM, NotificationTier.HYBRID):
                    tier_limits = TIER_LIMITS.get(UserTier(user_tier))
                    if tier_limits:
                        daily_llm_estimate = 0
                        for s in existing:
                            if s.notification_tier in (NotificationTier.LLM, NotificationTier.HYBRID):
                                try:
                                    s_interval = _rrule_interval_minutes(s.rrule)
                                    daily_llm_estimate += max(1, 1440 / s_interval)
                                except (ValueError, TypeError):
                                    daily_llm_estimate += 1
                        # Add estimate for the new schedule
                        try:
                            new_interval = _rrule_interval_minutes(rrule_str)
                            daily_llm_estimate += max(1, 1440 / new_interval)
                        except (ValueError, TypeError):
                            daily_llm_estimate += 1
                        if daily_llm_estimate > tier_limits.max_llm_notifications_per_day:
                            return _err(
                                f"Too many LLM notifications/day (~{int(daily_llm_estimate)}). "
                                f"Limit is {tier_limits.max_llm_notifications_per_day}/day. "
                                f"Use notification_tier='template' or reduce schedule frequency."
                            )

                # Minimum recurrence interval
                try:
                    interval = _rrule_interval_minutes(rrule_str)
                    if interval < tuning.min_schedule_interval_minutes:
                        return _err(
                            f"Schedule fires too frequently (~{int(interval)} min). "
                            f"Minimum interval is {tuning.min_schedule_interval_minutes} minutes. "
                            f"Use FREQ=HOURLY or less frequent."
                        )
                except (ValueError, TypeError):
                    pass  # Invalid RRULE — compute_next_trigger below provides the error

                # Parse RRULE in the user's local timezone so BYHOUR values
                # match what the user expects (e.g. "9am" = 9am local, not UTC).
                user_tz = safe_zoneinfo(user_timezone)
                try:
                    next_trigger = compute_next_trigger(rrule_str, user_tz)
                except (ValueError, TypeError) as e:
                    return _err(f"Invalid RRULE: {e}")

                if next_trigger is None:
                    return _err("RRULE produces no future occurrences")

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
                    # Minimum recurrence interval
                    try:
                        interval = _rrule_interval_minutes(args["rrule"])
                        if interval < tuning.min_schedule_interval_minutes:
                            return _err(
                                f"Schedule fires too frequently (~{int(interval)} min). "
                                f"Minimum interval is {tuning.min_schedule_interval_minutes} minutes."
                            )
                    except (ValueError, TypeError):
                        pass  # compute_next_trigger below provides the error

                    upd_tz = safe_zoneinfo(user_timezone)
                    try:
                        upd_next = compute_next_trigger(args["rrule"], upd_tz)
                    except (ValueError, TypeError) as e:
                        return _err(f"Invalid RRULE: {e}")
                    if upd_next is None:
                        return _err("RRULE produces no future occurrences")
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
                            llm_count = sum(
                                1 for s in all_schedules
                                if s.notification_tier in (NotificationTier.LLM, NotificationTier.HYBRID)
                            )
                            if llm_count >= tier_limits.max_llm_notifications_per_day:
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
        "send_notification",
        "Send a message to the user via Telegram. Max 2000 characters. "
        "Use for proactive sessions only.",
        {"message": str},
    )
    async def send_notification(args: dict[str, Any]) -> dict[str, Any]:
        message = args.get("message", "").strip()
        if not message:
            return _err("Notification message cannot be empty")
        if len(message) > 2000:
            return _err("Message too long (max 2000 characters)")
        html_err = _validate_telegram_html(message)
        if html_err:
            return _err(f"Invalid HTML in notification: {html_err}")
        if notification_sink is None:
            return _err("send_notification is not available in this session type")
        if notification_sink:
            preview = notification_sink[0][:80]
            return _err(
                f"Notification already queued: '{preview}...'. "
                "You must call send_notification exactly once per session."
            )
        notification_sink.append(message)
        return _ok({"status": "queued", "message": message, "user_id": user_id})

    @tool(
        "search_vocabulary",
        "Search the student's existing vocabulary by keyword or topic. "
        "Case-insensitive, returns up to 20 results.",
        {"query": str},
    )
    async def search_vocabulary(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query:
            return _err("Search query cannot be empty")

        async with session_factory() as db_session:
            results = await VocabularyRepo.search(db_session, user_id, query, limit=_VOCAB_SEARCH_LIMIT)
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
        limit = min(args.get("limit", 10), _EXERCISE_HISTORY_MAX)
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
        days = min(max(args.get("days", 30), 1), 90)

        async with session_factory() as db_session:
            try:
                score_7d = await ExerciseResultRepo.get_score_summary(
                    db_session, user_id, days=7,
                )
                score_period = await ExerciseResultRepo.get_score_summary(
                    db_session, user_id, days=days,
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

    # --- Build list and permission check ---

    all_tools = [
        get_user_profile,
        update_preference,
        record_exercise_result,
        add_vocabulary,
        get_due_vocabulary,
        manage_schedule,
        send_notification,
        search_vocabulary,
        get_exercise_history,
        get_progress_summary,
    ]

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
