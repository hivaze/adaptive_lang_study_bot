import time
import uuid
from datetime import datetime, timezone

from loguru import logger

from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.enums import CloseReason, Difficulty, PipelineStatus
from adaptive_lang_study_bot.metrics import PIPELINE_COMPLETED, PIPELINE_DURATION
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t
from adaptive_lang_study_bot.utils import compute_new_streak, strip_mcp_prefix, summarize_tool_usage, user_local_now

_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Prep tools that hint at what the agent was doing when the user dropped off.
_PREP_TOOL_HINTS = {
    "get_exercise_history": "preparing an exercise",
    "get_due_vocabulary": "setting up vocabulary review",
    "search_vocabulary": "looking up vocabulary",
    "get_progress_summary": "reviewing progress",
}

# Close reasons that mean the session ended without the user explicitly finishing.
# idle_timeout is included: the user stopped responding mid-task, which means
# unfinished work should be surfaced in the next session's continuation context.
_FORCED_CLOSE_REASONS = frozenset({
    CloseReason.TURN_LIMIT, CloseReason.COST_LIMIT,
    CloseReason.SHUTDOWN, CloseReason.ERROR,
    CloseReason.IDLE_TIMEOUT,
})


async def run_post_session(
    *,
    user_id: int,
    session_id: uuid.UUID,
    tools_called: list[str],
    close_reason: str = CloseReason.UNKNOWN,
    bot=None,  # aiogram Bot instance for immediate celebrations
) -> None:
    """Post-session validation pipeline. Pure Python, no LLM.

    Uses atomic UPDATE for all user field changes to avoid overwriting
    concurrent modifications from a new session's tool calls.

    When *bot* is provided, newly detected milestones are sent as Telegram
    messages immediately instead of being queued for the next session start.

    Steps:
    1. Validate profile integrity
    2. Update streak
    3. Auto-adjust difficulty
    4. Update last_activity
    5. Detect milestones (+ send immediate celebrations)
    6. Record analytics
    """
    issues: list[str] = []
    pipeline_start = time.monotonic()

    try:
        async with async_session_factory() as db:
            user = await UserRepo.get(db, user_id)
            if user is None:
                logger.error("Post-session: user {} not found", user_id)
                return

            # Collect all changes as a dict for a single atomic UPDATE
            # at the end, avoiding ORM dirty tracking that could overwrite
            # concurrent tool writes in a new session.
            updates: dict = {}

            # Snapshot mutable fields BEFORE any mutations so we can detect
            # if the user changed them via tool during the session.
            original_difficulty = user.preferred_difficulty

            # --- Step 1: Validate profile integrity ---
            if user.level not in _LEVELS:
                issues.append(f"Invalid level: {user.level}")
                updates["level"] = "A1"

            scores = user.recent_scores or []
            cleaned_scores = [s for s in scores if 0 <= s <= 10]
            if len(cleaned_scores) != len(scores):
                issues.append("Invalid scores found and cleaned")
                updates["recent_scores"] = cleaned_scores[-20:]

            if user.interests and len(user.interests) > tuning.max_interests:
                updates["interests"] = user.interests[:tuning.max_interests]
                issues.append(f"Interests capped at {tuning.max_interests}")

            if user.learning_goals and len(user.learning_goals) > tuning.max_learning_goals:
                updates["learning_goals"] = user.learning_goals[:tuning.max_learning_goals]
                issues.append(f"Learning goals capped at {tuning.max_learning_goals}")

            if user.weak_areas and len(user.weak_areas) > 10:
                updates["weak_areas"] = user.weak_areas[:10]
                issues.append("Weak areas capped at 10")

            if user.strong_areas and len(user.strong_areas) > 10:
                updates["strong_areas"] = user.strong_areas[:10]
                issues.append("Strong areas capped at 10")

            tool_names = [
                strip_mcp_prefix(tc) for tc in tools_called
            ]

            # --- Step 2: Update streak ---
            local_now = user_local_now(user)
            today = local_now.date()
            streak = compute_new_streak(user.streak_days, user.streak_updated_at, today)
            # Only advance the streak date when the session involved meaningful work
            # (at least one tool call). Chat-only sessions should not count.
            if user.streak_updated_at != today and tool_names:
                updates["streak_days"] = streak
                updates["streak_updated_at"] = today

            # --- Step 3: Auto-adjust difficulty ---
            # Only adjust at extremes. Don't reset to "normal" — that would
            # silently override explicit user preference set via /settings
            # or the update_preference tool.
            difficulty = user.preferred_difficulty
            recent = updates.get("recent_scores", user.recent_scores or [])
            window = tuning.difficulty_recent_window
            if len(recent) >= window:
                avg = sum(recent[-window:]) / window
                if avg >= tuning.difficulty_up_easy_normal and difficulty == Difficulty.EASY:
                    difficulty = Difficulty.NORMAL
                    issues.append(f"Auto-adjusted difficulty easy→normal (avg={avg:.1f})")
                elif avg >= tuning.difficulty_up_normal_hard and difficulty == Difficulty.NORMAL:
                    difficulty = Difficulty.HARD
                    issues.append(f"Auto-adjusted difficulty normal→hard (avg={avg:.1f})")
                elif avg <= tuning.difficulty_down_hard_normal and difficulty == Difficulty.HARD:
                    difficulty = Difficulty.NORMAL
                    issues.append(f"Auto-adjusted difficulty hard→normal (avg={avg:.1f})")
                elif avg <= tuning.difficulty_down_normal_easy and difficulty == Difficulty.NORMAL:
                    difficulty = Difficulty.EASY
                    issues.append(f"Auto-adjusted difficulty normal→easy (avg={avg:.1f})")

            if difficulty != user.preferred_difficulty:
                # Re-read user in a SEPARATE session to bypass SQLAlchemy's
                # identity map, which would return the cached object above.
                async with async_session_factory() as fresh_db:
                    fresh_user = await UserRepo.get(fresh_db, user_id)
                if fresh_user is None:
                    issues.append("User deleted during session, skipping difficulty auto-adjust")
                elif fresh_user.preferred_difficulty == original_difficulty:
                    # User hasn't changed preference during session — apply auto-adjust
                    updates["preferred_difficulty"] = difficulty
                else:
                    difficulty = fresh_user.preferred_difficulty
                    issues.append("Skipped difficulty auto-adjust: user changed preference during session")

            # Notify user of difficulty change via pending celebrations
            milestones = dict(user.milestones or {})
            pending = list(milestones.get("pending_celebrations", []))

            native_lang = user.native_language or DEFAULT_LANGUAGE
            if "preferred_difficulty" in updates:
                pending.append(
                    t("pipeline.difficulty_adjusted", native_lang,
                      old=user.preferred_difficulty, new=difficulty)
                )
                pending = pending[-tuning.pending_celebrations_cap:]

            # --- Step 4: Update last_activity ---
            now = datetime.now(timezone.utc)
            updates["last_session_at"] = now

            # Only count sessions with meaningful interaction.
            # Use SQL expression for atomic increment to avoid lost updates
            # if two post-session pipelines overlap for the same user.
            if tools_called:
                updates["sessions_completed"] = User.sessions_completed + 1

            # Build summary from tool usage
            summary_parts = summarize_tool_usage(tool_names)

            # Determine session status based on close reason
            status = (
                "incomplete"
                if close_reason in _FORCED_CLOSE_REASONS
                else "completed"
            )

            # Enrich with actual exercise data from this session
            exercises = await ExerciseResultRepo.get_by_session(db, session_id)

            last_exercise_type = None
            last_topic = None
            last_score = None
            all_words: list[str] = []
            all_topics: list[str] = []

            # Per-topic and per-exercise-type score tracking for error patterns
            topic_scores: dict[str, list[int]] = {}
            type_scores: dict[str, list[int]] = {}

            for ex in exercises:
                last_exercise_type = ex.exercise_type
                last_topic = ex.topic
                last_score = ex.score
                if ex.words_involved:
                    all_words.extend(ex.words_involved)
                if ex.topic and ex.topic not in all_topics:
                    all_topics.append(ex.topic)
                if ex.topic:
                    topic_scores.setdefault(ex.topic, []).append(ex.score)
                if ex.exercise_type:
                    type_scores.setdefault(ex.exercise_type, []).append(ex.score)

            # Enrich summary with real data
            if exercises and not summary_parts:
                summary_parts.append("Completed exercises")
            if all_topics:
                summary_parts.append(f"Topics: {', '.join(all_topics[:5])}")

            activity: dict = {
                "type": "session",
                "status": status,
                "close_reason": close_reason,
                "exercise_count": len(exercises),
                "session_summary": ". ".join(summary_parts) if summary_parts else "Practice session",
                "tools_used": tool_names[:10],
            }
            if last_exercise_type:
                activity["last_exercise"] = last_exercise_type
            if last_topic:
                activity["topic"] = last_topic
            if last_score is not None:
                activity["score"] = last_score
            if all_words:
                unique_words = list(dict.fromkeys(all_words))
                activity["words_practiced"] = [w[:100] for w in unique_words[:20]]
            if all_topics:
                activity["topics_covered"] = all_topics[:10]

            # Error patterns: topics where user scored poorly (avg <= 5)
            struggling = [
                {"topic": tp, "avg_score": round(sum(sc) / len(sc), 1)}
                for tp, sc in topic_scores.items()
                if sum(sc) / len(sc) <= 5
            ]
            if struggling:
                struggling.sort(key=lambda x: x["avg_score"])
                activity["struggling_topics"] = struggling[:5]

            # Weak/strong areas are updated exclusively by the record_exercise_result
            # tool during the session (tools.py:345-377), which has better data:
            # it queries the last 5 topic-specific scores across sessions and uses
            # tuning.weak_area_min_occurrences. The pipeline only validates/caps
            # array lengths (Step 1 above).

            # Per-exercise-type average scores for continuity
            if type_scores:
                activity["exercise_type_scores"] = {
                    tp: round(sum(sc) / len(sc), 1)
                    for tp, sc in type_scores.items()
                }

            # Infer pending context when session ended with no completed exercises
            # but the agent was clearly preparing something (called prep tools).
            # This helps the next session know what was "in progress" when the
            # user dropped off.
            if not exercises and close_reason == CloseReason.IDLE_TIMEOUT and tool_names:
                hints = [
                    _PREP_TOOL_HINTS[tc] for tc in tool_names
                    if tc in _PREP_TOOL_HINTS
                ]
                if hints:
                    activity["pending_context"] = hints[0]

            updates["last_activity"] = activity

            # Append compact entry to session_history (rolling last 5)
            history = list(user.session_history or [])
            history_entry: dict = {
                "date": local_now.strftime("%Y-%m-%d %H:%M"),
                "summary": activity.get("session_summary", "Practice session"),
                "status": status,
                "close_reason": close_reason,
            }
            if all_topics:
                history_entry["topics"] = all_topics[:5]
            if last_score is not None:
                history_entry["score"] = last_score
            history.append(history_entry)
            updates["session_history"] = history[-tuning.session_history_cap:]

            # --- Step 5: Detect milestones ---
            # Track fired milestones to prevent re-firing across sessions.
            # Each set is persisted in the milestones JSONB column.
            fired_streaks: set[int] = set(milestones.get("fired_streaks", []))
            fired_vocab: set[int] = set(milestones.get("fired_vocab", []))
            fired_sessions: set[int] = set(milestones.get("fired_sessions", []))

            # Streak milestones
            if streak > 0 and streak in tuning.milestone_streak and streak not in fired_streaks:
                msg = t("pipeline.milestone_streak", native_lang, streak=streak)
                if msg not in pending:
                    pending.append(msg)
                fired_streaks.add(streak)
            milestones["fired_streaks"] = sorted(fired_streaks)

            # Vocabulary milestones
            actual_count = await VocabularyRepo.count_for_user(db, user_id)
            if user.vocabulary_count != actual_count:
                updates["vocabulary_count"] = actual_count

            # Compute pre-session vocab count by subtracting words added
            # during this session.  user.vocabulary_count already includes
            # the session's add_vocabulary increments (atomic UPDATE in tool),
            # so using it as prev_count would make prev == actual and
            # milestones crossed during the session would never fire.
            session_record = await SessionRepo.get(db, session_id)
            if session_record is not None:
                session_vocab_added = await VocabularyRepo.count_added_since(
                    db, user_id, session_record.started_at,
                )
            else:
                session_vocab_added = 0
            prev_count = max(actual_count - session_vocab_added, 0)

            for threshold in tuning.milestone_vocab:
                if prev_count < threshold <= actual_count and threshold not in fired_vocab:
                    msg = t("pipeline.milestone_vocab", native_lang, count=actual_count)
                    if msg not in pending:
                        pending.append(msg)
                    fired_vocab.add(threshold)
            milestones["fired_vocab"] = sorted(fired_vocab)

            # Session milestones
            new_sessions = (user.sessions_completed or 0) + (1 if tools_called else 0)
            if new_sessions in tuning.milestone_sessions and new_sessions not in fired_sessions:
                msg = t("pipeline.milestone_sessions", native_lang, count=new_sessions)
                if msg not in pending:
                    pending.append(msg)
                fired_sessions.add(new_sessions)
            milestones["fired_sessions"] = sorted(fired_sessions)

            # Send immediate celebrations if bot is available
            old_pending = list((user.milestones or {}).get("pending_celebrations", []))
            new_celebrations = [m for m in pending if m not in old_pending]

            if bot is not None and new_celebrations:
                sent_celebrations: list[str] = []
                for celebration_msg in new_celebrations:
                    try:
                        await bot.send_message(user_id, celebration_msg)
                        sent_celebrations.append(celebration_msg)
                    except Exception:
                        logger.debug("Could not send immediate celebration to user {}", user_id)

                # Only remove celebrations that were actually delivered.
                # Failed sends stay in pending so they appear at next session start.
                pending = [m for m in pending if m not in sent_celebrations]

            milestones["pending_celebrations"] = pending[-tuning.pending_celebrations_cap:]  # Keep last 5
            milestones["vocabulary_count"] = actual_count
            milestones["days_streak"] = streak

            # --- Step 6: Record analytics ---
            pipeline_status = PipelineStatus.COMPLETED
            pipeline_issues = {"issues": issues} if issues else None

            await SessionRepo.set_pipeline_status(
                db, session_id, pipeline_status, pipeline_issues,
            )

            # Single atomic UPDATE for all user field changes
            await UserRepo.update_fields(db, user_id, **updates)

            # Merge milestones atomically (uses || operator to preserve
            # concurrent changes from session_manager celebrations clearing)
            await UserRepo.update_milestones(db, user_id, milestones)
            await db.commit()

        PIPELINE_COMPLETED.labels(status="completed").inc()
        PIPELINE_DURATION.observe(time.monotonic() - pipeline_start)

        if issues:
            logger.warning(
                "Post-session pipeline for user {}: {} issues: {}",
                user_id, len(issues), issues,
            )
        else:
            logger.info("Post-session pipeline completed for user {}", user_id)

    except Exception:
        PIPELINE_COMPLETED.labels(status="failed").inc()
        PIPELINE_DURATION.observe(time.monotonic() - pipeline_start)
        logger.exception("Post-session pipeline failed for user {}", user_id)
        try:
            async with async_session_factory() as db:
                await SessionRepo.set_pipeline_status(
                    db, session_id, PipelineStatus.FAILED, {"error": "pipeline_exception"},
                )
                await db.commit()
        except Exception:
            logger.exception("Failed to update pipeline status for session {}", session_id)
