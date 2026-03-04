import asyncio
import os
import time
import uuid
from contextlib import AsyncExitStack
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.agent.hooks import (
    TURN_LIMIT_WARN_FRACTION,
    SessionHookState,
    build_session_hooks,
)
from adaptive_lang_study_bot.agent.pool import session_pool
from adaptive_lang_study_bot.agent.prompt_builder import (
    build_proactive_prompt,
    build_summary_prompt,
    build_system_prompt,
    compute_session_context,
)
from adaptive_lang_study_bot.agent.tools import (
    compute_plan_progress,
    create_langbot_server,
    create_session_tools,
    fetch_news_for_proactive,
    web_search_available,
)
from adaptive_lang_study_bot.cache.session_lock import (
    acquire_session_lock,
    refresh_session_lock,
    release_session_lock,
)
from adaptive_lang_study_bot.config import TIER_LIMITS, TierLimits, tuning
from sqlalchemy import update

from adaptive_lang_study_bot.enums import CloseReason, PipelineStatus, SessionType, UserTier
from adaptive_lang_study_bot.db.engine import async_session_factory
from adaptive_lang_study_bot.db.models import Session, User
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    LearningPlanRepo,
    ScheduleRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.bot.helpers import (
    localize_value,
    markdown_to_telegram_html,
    split_agent_sections,
)
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t
from adaptive_lang_study_bot.metrics import (
    MESSAGE_COST_USD,
    MESSAGES_PROCESSED,
    SESSION_COST_USD,
    SESSION_DURATION_SECONDS,
    SESSION_ERRORS,
    SESSIONS_CLOSED,
    SESSIONS_CREATED,
)
from adaptive_lang_study_bot.pipeline.post_session import run_post_session
from adaptive_lang_study_bot.utils import compute_level_progress, strip_mcp_prefix

# Remove CLAUDECODE env var once at import time so nested SDK subprocesses
# can start (instead of popping it on every session creation).
os.environ.pop("CLAUDECODE", None)


def _log_task_exception(task: asyncio.Task) -> None:
    """Callback for background tasks to ensure exceptions are logged."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task failed: {}", task.exception())


async def _compute_stale_topics(
    db: AsyncSession, user_id: int,
) -> tuple[list[dict], dict[str, dict]]:
    """Compute stale topics and topic performance from recent exercises.

    Returns a tuple of:
    - stale_topics: topics not practiced recently with low scores
    - topic_performance: all topics with ``{"avg_score": float, "count": int}``
    """
    recent_results = await ExerciseResultRepo.get_recent(db, user_id, limit=tuning.stale_topic_exercise_window)
    if not recent_results:
        return [], {}

    topic_data: dict[str, dict] = {}
    for ex in recent_results:
        if not ex.topic:
            continue
        if ex.topic not in topic_data:
            topic_data[ex.topic] = {
                "topic": ex.topic,
                "last_at": ex.created_at,
                "scores": [],
            }
        topic_data[ex.topic]["scores"].append(ex.score)
        if ex.created_at > topic_data[ex.topic]["last_at"]:
            topic_data[ex.topic]["last_at"] = ex.created_at

    # Build topic_performance dict for ALL topics
    topic_performance: dict[str, dict] = {}
    for topic, data in topic_data.items():
        scores = data["scores"]
        topic_performance[topic] = {
            "avg_score": round(sum(scores) / len(scores), 1),
            "count": len(scores),
        }

    # Filter stale topics (not practiced recently + low avg score)
    now = datetime.now(timezone.utc)
    stale: list[dict] = []
    for data in topic_data.values():
        days_ago = (now - data["last_at"]).total_seconds() / 86400
        if days_ago >= tuning.stale_topic_days:
            avg_score = sum(data["scores"]) / len(data["scores"])
            if avg_score <= tuning.stale_topic_score:
                stale.append({
                    "topic": data["topic"],
                    "days_ago": round(days_ago, 1),
                    "avg_score": avg_score,
                })

    stale.sort(key=lambda x: x["avg_score"])
    return stale, topic_performance


async def run_proactive_llm_session(
    user: User,
    session_type: str,
    trigger_data: dict,
) -> tuple[str | None, float]:
    """Run a short-lived proactive LLM session to generate a notification.

    Returns ``(message_text, cost_usd)``.  *message_text* is ``None`` when
    the session fails or the LLM returns no text.

    Tool-less: all required data is pre-fetched and embedded in the prompt.
    The LLM's text output is extracted directly (no MCP tools needed).

    This is a standalone function — proactive sessions are short-lived and
    do not go through :class:`SessionManager` (no idle cleanup, no user
    interaction).  A Redis session lock is acquired to prevent concurrent
    sessions (interactive or proactive) for the same user.
    """
    user_id = user.telegram_id

    # 1. Acquire proactive pool slot (non-blocking)
    acquired = await session_pool.acquire_proactive()
    if not acquired:
        logger.warning("No proactive pool slots available for user {}", user_id)
        return None, 0.0

    # 2. Acquire Redis session lock (prevents concurrent sessions for the same user)
    lock_token = await acquire_session_lock(user_id, ttl_seconds=60)
    if lock_token is None:
        await session_pool.release_proactive()
        logger.debug("Skipping proactive session for user {} — session lock held", user_id)
        return None, 0.0

    accumulated_cost = 0.0
    num_turns = 0
    text_parts: list[str] = []
    db_session_id = uuid.uuid4()
    client: ClaudeSDKClient | None = None
    sdk_started = False

    try:
        # 3. Pre-fetch data for prompt embedding
        active_plan = None
        plan_progress = None
        prefetch: dict = {}

        async with async_session_factory() as db:
            if session_type == SessionType.PROACTIVE_REVIEW:
                cards = await VocabularyRepo.get_due(db, user_id, limit=10)
                prefetch["due_vocabulary"] = [
                    {"word": c.word, "translation": c.translation, "topic": c.topic,
                     "review_count": c.review_count}
                    for c in cards
                ]
            elif session_type == SessionType.PROACTIVE_SUMMARY:
                active_plan = await LearningPlanRepo.get_active(db, user_id)
                if active_plan:
                    all_plan_topics = [
                        t
                        for p in (active_plan.plan_data or {}).get("phases", [])
                        for t in p.get("topics", [])
                    ]
                    topic_stats = await ExerciseResultRepo.get_stats_for_topics(
                        db, user_id, all_plan_topics, active_plan.start_date,
                    )
                    plan_progress = compute_plan_progress(
                        active_plan.plan_data or {},
                        active_plan.total_weeks,
                        active_plan.start_date,
                        topic_stats,
                    )

                score_7d = await ExerciseResultRepo.get_score_summary(db, user_id, days=7)
                score_30d = await ExerciseResultRepo.get_score_summary(db, user_id, days=30)
                topic_perf = await ExerciseResultRepo.get_topic_stats(db, user_id, days=30)
                vocab_states = await VocabularyRepo.get_state_counts(db, user_id)
                sessions_week = await SessionRepo.get_activity_stats(db, user_id, days=7)
                prefetch["progress_summary"] = {
                    "score_7d": score_7d,
                    "score_30d": score_30d,
                    "topic_performance": topic_perf,
                    "vocabulary": {
                        "new": vocab_states.get(0, 0),
                        "learning": vocab_states.get(1, 0),
                        "review": vocab_states.get(2, 0),
                        "relearning": vocab_states.get(3, 0),
                    },
                    "sessions_this_week": sessions_week["session_count"],
                }

        # 3b. Optionally fetch news for proactive prompt enrichment
        news_context = await fetch_news_for_proactive(
            target_language=user.target_language or "",
            interests=user.interests,
        )
        if news_context:
            prefetch["news_context"] = news_context

        # 4. Build proactive system prompt
        system_prompt = build_proactive_prompt(
            user, session_type, trigger_data,
            active_plan=active_plan, plan_progress=plan_progress,
            prefetch=prefetch,
        )

        # 5. Create DB session record
        async with async_session_factory() as db:
            await SessionRepo.create(
                db,
                id=db_session_id,
                user_id=user_id,
                session_type=session_type,
                system_prompt=system_prompt,
            )
            await db.commit()

        # 6. Create SDK client (haiku, no hooks, no tools)
        options = ClaudeAgentOptions(
            model=tuning.proactive_model,
            max_turns=tuning.proactive_max_turns,
            thinking={"type": tuning.proactive_thinking},
            effort=tuning.proactive_effort,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
        )
        client = ClaudeSDKClient(options)

        # 7. Run with timeout — extract TextBlock output directly
        async with asyncio.timeout(tuning.proactive_session_timeout_seconds):
            await client.__aenter__()
            sdk_started = True
            await client.query(
                "Generate the notification message now. Write it directly as your response. "
                "Use Markdown formatting (**bold**, *italic*, `code`). Do NOT call any tools."
            )
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    accumulated_cost += msg.total_cost_usd or 0
                    if msg.num_turns is not None:
                        num_turns = msg.num_turns

    except TimeoutError:
        logger.warning(
            "Proactive LLM session timed out for user {} (type={})",
            user_id, session_type,
        )
    except Exception:
        logger.exception(
            "Proactive LLM session failed for user {} (type={})",
            user_id, session_type,
        )
    finally:
        # Close SDK client
        if client is not None and sdk_started:
            await _close_standalone_sdk_client(client, f"Proactive (user {user_id})")

        # Release Redis session lock
        try:
            await release_session_lock(user_id, lock_token)
        except Exception:
            logger.warning("Failed to release proactive session lock for user {}", user_id)

        # Release pool slot
        try:
            await session_pool.release_proactive()
        except Exception:
            logger.warning("Failed to release proactive pool slot for user {}", user_id)

        # Update session record (mark pipeline completed — proactive sessions
        # don't run the post-session pipeline, so skip straight to completed)
        try:
            async with async_session_factory() as db:
                await SessionRepo.update_end(
                    db,
                    db_session_id,
                    cost_usd=accumulated_cost,
                    num_turns=num_turns,
                    tool_calls_count=0,
                )
                await db.execute(
                    update(Session)
                    .where(Session.id == db_session_id)
                    .values(pipeline_status=PipelineStatus.COMPLETED)
                )
                await db.commit()
        except Exception:
            logger.warning("Failed to update proactive session record for user {}", user_id)

    tier = UserTier(user.tier)
    SESSIONS_CREATED.labels(tier=tier.value, session_type=session_type).inc()
    if accumulated_cost > 0:
        SESSION_COST_USD.labels(tier=tier.value, session_type=session_type).observe(accumulated_cost)

    # Extract message from LLM text output
    message_text = "\n".join(text_parts).strip() or None

    # Convert any Markdown in LLM output to Telegram HTML, enforce length limit
    if message_text:
        message_text = markdown_to_telegram_html(message_text)
        if len(message_text) > tuning.notification_max_length:
            message_text = message_text[:tuning.notification_max_length - 3] + "..."

    if message_text is None:
        logger.warning(
            "Proactive LLM session for user {} (type={}) produced no valid message "
            "(cost=${:.4f}, falling back to template)",
            user_id, session_type, accumulated_cost,
        )
    else:
        logger.info(
            "Proactive LLM session completed: user={} type={} cost=${:.4f}",
            user_id, session_type, accumulated_cost,
        )
    return message_text, accumulated_cost


async def run_summary_llm_session(
    native_language: str,
    target_language: str,
    session_data: dict,
    close_reason: str,
    user_name: str,
    user_streak: int,
    user_level: str,
    user_timezone: str = "UTC",
    plan_summary: str | None = None,
    level_progress: str | None = None,
) -> tuple[str | None, float]:
    """Run a short-lived LLM session to generate an AI session summary.

    Returns ``(summary_text, cost_usd)``.  *summary_text* is ``None`` when
    the session fails or the agent returns nothing.

    Tool-less, no DB writes, no Redis lock. Uses a proactive pool slot.
    """
    accumulated_cost = 0.0
    client: ClaudeSDKClient | None = None
    sdk_started = False

    # Acquire proactive pool slot (non-blocking)
    acquired = await session_pool.acquire_proactive()
    if not acquired:
        logger.debug("No proactive pool slots for summary generation")
        return None, 0.0

    try:
        system_prompt = build_summary_prompt(
            native_language,
            target_language,
            session_data=session_data,
            close_reason=close_reason,
            user_name=user_name,
            user_streak=user_streak,
            user_level=user_level,
            user_timezone=user_timezone,
            plan_summary=plan_summary,
            level_progress=level_progress,
        )

        options = ClaudeAgentOptions(
            model=tuning.proactive_model,
            max_turns=tuning.summary_max_turns,
            thinking={"type": tuning.summary_thinking},
            effort=tuning.summary_effort,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
        )
        client = ClaudeSDKClient(options)

        text_parts: list[str] = []
        async with asyncio.timeout(tuning.summary_session_timeout_seconds):
            await client.__aenter__()
            sdk_started = True
            await client.query(
                "Generate the session summary now using ONLY the session data "
                "provided above. Do NOT ask for additional information."
            )
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    accumulated_cost += msg.total_cost_usd or 0

        summary_text = "\n".join(text_parts).strip() or None
        if summary_text:
            logger.info(
                "AI session summary generated (cost=${:.4f})",
                accumulated_cost,
            )
        return summary_text, accumulated_cost

    except TimeoutError:
        logger.warning("Summary LLM session timed out")
        return None, accumulated_cost
    except Exception:
        logger.warning("Summary LLM session failed", exc_info=True)
        return None, accumulated_cost
    finally:
        if client is not None and sdk_started:
            await _close_standalone_sdk_client(client, "Summary")

        try:
            await session_pool.release_proactive()
        except Exception:
            logger.warning("Failed to release proactive pool slot for summary")


def _collect_session_data(managed: "ManagedSession") -> dict:
    """Extract summary-relevant data from a managed session into a plain dict."""
    tool_names = [strip_mcp_prefix(tc) for tc in managed.tools_called]
    hook = managed.hook_state

    if hook and hook.exercise_scores:
        # exercise_scores may contain pre-seeded scores from prior sessions
        # (used for adaptive hint continuity). Only count new ones.
        session_scores = hook.exercise_scores[hook._seeded_count:]
        exercise_count = len(session_scores)
    else:
        session_scores = []
        exercise_count = tool_names.count("record_exercise_result")

    return {
        "exercise_count": exercise_count,
        "exercise_scores": session_scores,
        "exercise_topics": list(hook.exercise_topics) if hook else [],
        "exercise_types": list(hook.exercise_types) if hook else [],
        "words_added": list(hook.words_added) if hook else [],
        "words_reviewed": hook.words_reviewed if hook else 0,
        "vocab_count": tool_names.count("add_vocabulary"),
        "turn_count": managed.turn_count,
        "duration_minutes": int((time.time() - managed.started_at) / 60),
    }


def _build_summary_cta_keyboard(
    lang: str,
    *,
    has_vocabulary: bool = True,
    limit_close: bool = False,
) -> InlineKeyboardMarkup:
    """Build CTA keyboard for session summaries.

    When *limit_close* is True (turn/cost limit hit), the primary button uses
    "Continue lesson" wording so the user knows the session was interrupted
    rather than naturally finished.
    """
    cta_key = "cta.continue_lesson" if limit_close else "cta.start_session"
    rows = [
        [InlineKeyboardButton(text=t(cta_key, lang), callback_data="cta:session")],
    ]
    if has_vocabulary:
        rows.append(
            [InlineKeyboardButton(text=t("cta.start_review", lang), callback_data="cta:words")],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Close reasons that should use AI-generated summaries.
_AI_SUMMARY_REASONS = frozenset({
    CloseReason.TURN_LIMIT,
    CloseReason.COST_LIMIT,
    CloseReason.IDLE_TIMEOUT,
    CloseReason.EXPLICIT_CLOSE,
})


async def _generate_and_send_summary(
    bot: Bot,
    user_id: int,
    lang: str,
    session_data: dict,
    close_reason: str,
    managed: "ManagedSession",
    user_name: str,
    user_streak: int,
    user_level: str,
    target_language: str,
    skip_if_active_fn: "Callable[[], bool] | None" = None,
) -> None:
    """Generate AI summary and send to user. Falls back to enriched template.

    When *skip_if_active_fn* is provided and returns ``True`` right before
    sending, the summary is silently dropped.  This prevents stale summaries
    from arriving after a new session has already started (cleanup-loop race).

    All exceptions are caught internally — safe for fire-and-forget via
    ``asyncio.create_task``.
    """
    try:
        has_progress = bool(
            session_data["exercise_count"]
            or session_data["vocab_count"]
            or session_data.get("words_reviewed", 0)
        )

        # Fetch plan context and level progress for the summary (best-effort)
        plan_summary: str | None = None
        level_progress: str | None = None
        try:
            async with async_session_factory() as db:
                # Level progress
                user_fresh = await UserRepo.get(db, user_id)
                if user_fresh and user_fresh.recent_scores:
                    level_progress = compute_level_progress(
                        user_fresh.recent_scores, user_fresh.level,
                    )

                active_plan = await LearningPlanRepo.get_active(db, user_id)
                if active_plan:
                    all_topics = [
                        t_name
                        for p in (active_plan.plan_data or {}).get("phases", [])
                        for t_name in p.get("topics", [])
                    ]
                    topic_stats = await ExerciseResultRepo.get_stats_for_topics(
                        db, user_id, all_topics, active_plan.start_date,
                    )
                    progress = compute_plan_progress(
                        active_plan.plan_data or {},
                        active_plan.total_weeks,
                        active_plan.start_date,
                        topic_stats,
                    )
                    plan_summary = (
                        f"{active_plan.current_level}\u2192{active_plan.target_level}, "
                        f"Week {progress.get('current_week', '?')}/{active_plan.total_weeks}, "
                        f"{progress.get('progress_pct', 0)}% complete "
                        f"({progress.get('completed_topics', 0)}/{progress.get('total_topics', 0)} topics)"
                    )
        except Exception:
            logger.debug("Failed to fetch plan context for summary (user={})", user_id)

        # Try AI summary
        summary_text: str | None = None
        if close_reason in _AI_SUMMARY_REASONS:
            summary_text, _cost = await run_summary_llm_session(
                native_language=lang,
                target_language=target_language,
                session_data=session_data,
                close_reason=close_reason,
                user_name=user_name,
                user_streak=user_streak,
                user_level=user_level,
                user_timezone=managed.user_timezone,
                plan_summary=plan_summary,
                level_progress=level_progress,
            )

        # Fallback to enriched template
        if summary_text is None:
            summary_text = _build_template_summary(managed, session_data)

        # If the user has already started a new session, drop the stale summary.
        if skip_if_active_fn is not None and skip_if_active_fn():
            logger.debug("Suppressing stale summary for user {} — new session active", user_id)
            return

        # Attach CTA keyboard.
        # - For limit closes (turn/cost): always attach so the user can continue
        #   in a new session (their last message was not processed).
        # - For other closes: only attach for no-progress sessions.
        is_limit_close = close_reason in {CloseReason.TURN_LIMIT, CloseReason.COST_LIMIT}
        total_vocab = managed.user_vocabulary_count + session_data.get("vocab_count", 0)
        reply_markup = (
            _build_summary_cta_keyboard(
                lang, has_vocabulary=total_vocab > 0, limit_close=is_limit_close,
            )
            if is_limit_close or not has_progress
            else None
        )

        # Split on --- delimiters first, then convert each section to HTML
        sections = [markdown_to_telegram_html(s) for s in split_agent_sections(summary_text)]
        for i, section in enumerate(sections):
            # Attach CTA keyboard only to the last section
            markup = reply_markup if i == len(sections) - 1 else None
            try:
                await bot.send_message(
                    user_id,
                    section,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except TelegramBadRequest:
                try:
                    await bot.send_message(
                        user_id, section, parse_mode=None, reply_markup=markup,
                    )
                except Exception:
                    logger.warning("Failed to send summary to user {} (plain text fallback)", user_id)
    except Exception:
        logger.warning("Summary generation failed for user {}", user_id, exc_info=True)


def _build_template_summary(managed: "ManagedSession", session_data: dict | None = None) -> str:
    """Build an enriched template summary — used as fallback when AI summary fails.

    Accepts pre-computed *session_data* to avoid recomputing what
    ``_collect_session_data`` already produced.
    """
    lang = managed.native_language
    if session_data is None:
        session_data = _collect_session_data(managed)

    parts = [t("session.summary_header", lang)]

    exercise_count = session_data["exercise_count"]
    vocab_count = session_data["vocab_count"]
    words_reviewed = session_data.get("words_reviewed", 0)
    turn_count = session_data.get("turn_count", 0)

    # Sanity check: each exercise needs the student to answer in a separate turn.
    # Minimum turns = 1 (initial) + 1 per exercise answered. If turns are too low,
    # the agent likely scored exercises in the same turn it presented them.
    if exercise_count and turn_count < exercise_count + 1:
        logger.debug(
            "Template summary sanity: {} exercises but only {} turns — suppressing exercise count",
            exercise_count, turn_count,
        )
        exercise_count = 0

    if exercise_count:
        parts.append(t("session.summary_exercises", lang, count=exercise_count))
        if session_data["exercise_topics"]:
            unique_topics = list(dict.fromkeys(session_data["exercise_topics"]))[:5]
            parts.append(t("session.summary_topics", lang, topics=", ".join(unique_topics)))
    if vocab_count:
        parts.append(t("session.summary_vocab", lang, count=vocab_count))
        if session_data["words_added"]:
            sample = session_data["words_added"][:5]
            parts.append(t("session.summary_words_sample", lang, words=", ".join(sample)))
    if words_reviewed:
        parts.append(t("session.summary_reviews", lang, count=words_reviewed))
    if not (exercise_count or vocab_count or words_reviewed):
        parts.append(t("session.summary_no_progress", lang))

    parts.append(t("session.summary_footer", lang))
    return "\n".join(parts)


@dataclass
class ManagedSession:
    """Holds the state for a single active agent session.

    Warning flags lifecycle (each starts ``False`` and flips to ``True`` once):
    - ``limit_warned``: set in ``handle_message`` when remaining turns <= threshold
    - ``cost_warned``: set in ``handle_message`` when cost >= 80% of session max
    - ``idle_warned``: set in ``_cleanup_loop`` when idle time >= 70% of timeout
    These flags are one-shot: once set they prevent duplicate warnings.
    """

    client: ClaudeSDKClient
    user_id: int
    db_session_id: uuid.UUID  # Row in sessions table
    tier: UserTier = UserTier.FREE
    hook_state: SessionHookState | None = None
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    turn_count: int = 0
    accumulated_cost: float = 0.0
    tools_called: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    native_language: str = "en"  # For localized user-facing messages
    target_language: str = ""  # For summary prompt context
    user_timezone: str = "UTC"  # For user-local timestamps in summaries
    first_name: str = ""  # For personalized summaries
    user_level: str = "A1"  # For summary prompt context
    user_streak: int = 0  # For summary prompt context
    user_vocabulary_count: int = 0  # For CTA keyboard (suppress review when 0)
    session_type: str = SessionType.INTERACTIVE  # For metrics labels
    lock_token: str = ""  # Redis session lock owner token
    is_proactive: bool = False
    limit_warned: bool = False  # One-shot: turn limit approaching
    cost_warned: bool = False   # One-shot: cost limit approaching
    idle_warned: bool = False   # One-shot: idle timeout approaching
    last_message_debug: dict[str, object] | None = None
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack)


async def _force_close_sdk_client(client: ClaudeSDKClient) -> None:
    """Close SDK client without going through anyio TaskGroup ``__aexit__``.

    The normal path (``client.__aexit__`` → ``query.close()`` →
    ``tg.__aexit__``) raises ``RuntimeError: Attempted to exit cancel scope
    in a different task`` when called from a different asyncio task than the
    one that created the session.  This helper bypasses the TaskGroup exit
    and terminates the subprocess directly via the transport.
    """
    query = getattr(client, "_query", None)
    if query is None:
        return

    # Signal the read loop to stop.
    query._closed = True

    # Cancel running tasks inside the TaskGroup without trying to __aexit__.
    tg = getattr(query, "_tg", None)
    if tg is not None:
        try:
            tg.cancel_scope.cancel()
        except Exception:
            pass
        # Detach so a later accidental close() doesn't retry __aexit__.
        query._tg = None

    # Close the subprocess transport (sends SIGTERM + waits).
    transport = getattr(query, "transport", None)
    if transport is not None:
        try:
            await transport.close()
        except Exception:
            pass

    # Clear references so the client is inert.
    client._query = None
    client._transport = None


def _kill_sdk_subprocess(client: ClaudeSDKClient) -> None:
    """Last-resort synchronous SIGKILL for a leaked subprocess."""
    try:
        transport = getattr(client, "_transport", None) or (
            getattr(getattr(client, "_query", None), "transport", None)
        )
        if transport is not None:
            proc = getattr(transport, "_process", None)
            if proc is not None and proc.returncode is None:
                proc.kill()
    except Exception:
        pass


async def _close_standalone_sdk_client(client: ClaudeSDKClient, label: str = "") -> None:
    """Close an SDK client that was started via ``__aenter__`` in the same task.

    Used by proactive and summary sessions.  Interactive sessions close from
    a different asyncio task and must use ``_force_close_sdk_client`` instead.
    """
    try:
        await asyncio.wait_for(
            client.__aexit__(None, None, None),
            timeout=tuning.sdk_close_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("{} SDK close timed out", label)
        _kill_sdk_subprocess(client)
    except Exception:
        logger.warning("Error closing {} SDK client", label)
        _kill_sdk_subprocess(client)


class SessionManager:
    """Manages Claude SDK session lifecycle for all users."""

    def __init__(self) -> None:
        self._sessions: dict[int, ManagedSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._bot: Bot | None = None

    @staticmethod
    async def _release_lock_and_pool(user_id: int, is_proactive: bool, lock_token: str = "") -> None:
        """Release Redis session lock and pool slot, ignoring errors."""
        try:
            await release_session_lock(user_id, lock_token)
        except Exception:
            logger.warning("Failed to release Redis session lock for user {}", user_id)
        try:
            if is_proactive:
                await session_pool.release_proactive()
            else:
                await session_pool.release_interactive()
        except Exception:
            logger.warning("Failed to release pool slot for user {}", user_id)

    async def start(self, bot: Bot | None = None) -> None:
        """Start the background cleanup task."""
        self._bot = bot
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("SessionManager started")

    async def stop(self) -> None:
        """Stop all sessions and the cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Close all active sessions
        async with self._lock:
            for user_id in list(self._sessions.keys()):
                await self._close_session(user_id, reason=CloseReason.SHUTDOWN)
        logger.info("SessionManager stopped")

    async def _check_daily_limits(
        self,
        user_id: int,
        user_timezone: str,
        lang: str,
        tier: UserTier,
        limits: "TierLimits",
    ) -> str | None:
        """Check daily session and cost limits. Returns error message or None."""
        # Enforce daily session limit (0 = unlimited)
        if limits.max_sessions_per_day > 0:
            try:
                async with async_session_factory() as db:
                    today_count = await SessionRepo.count_today(
                        db, user_id, user_timezone=user_timezone,
                    )
                if today_count >= limits.max_sessions_per_day:
                    return t("session.daily_limit", lang,
                             used=today_count,
                             max_sessions=limits.max_sessions_per_day,
                             tier=localize_value(tier.value, lang))
            except SQLAlchemyError:
                logger.warning("Failed to check daily session count for user {}", user_id)
                return t("session.verify_error", lang)

        # Enforce daily cost limit
        try:
            async with async_session_factory() as db:
                today_cost = await SessionRepo.get_total_cost_today(
                    db, user_id, user_timezone=user_timezone,
                )
            if today_cost >= limits.max_cost_per_day_usd:
                return t("session.cost_limit", lang)
        except SQLAlchemyError:
            logger.warning("Failed to check daily cost for user {}", user_id)
            return t("session.cost_verify_error", lang)

        return None

    def _inject_limit_warnings(
        self,
        managed: "ManagedSession",
        limits: "TierLimits",
        lang: str,
        response_chunks: list[str],
    ) -> None:
        """Append turn/cost limit warnings to response if thresholds are reached."""
        user_id = managed.user_id
        # Warn when approaching turn limit
        if self._sessions.get(user_id) is managed and not managed.limit_warned:
            remaining = limits.max_turns_per_session - managed.turn_count
            threshold = max(2, int(limits.max_turns_per_session * TURN_LIMIT_WARN_FRACTION))
            if 0 < remaining <= threshold:
                managed.limit_warned = True
                response_chunks.append(t("session.turn_limit_warn", lang))

        # Warn when approaching per-session cost limit (80% threshold)
        if self._sessions.get(user_id) is managed and not managed.cost_warned:
            cost_threshold = limits.max_cost_per_session_usd * 0.8
            if managed.accumulated_cost >= cost_threshold:
                managed.cost_warned = True
                response_chunks.append(t("session.cost_warn", lang))

    async def handle_message(
        self,
        user: User,
        text: str,
    ) -> list[str] | None:
        """Handle a user message. Returns list of response text chunks.

        Returns ``None`` when the session was closed due to turn/cost limits —
        meaning a summary with CTA buttons has been sent to the user and the
        caller should NOT send any additional messages.
        """
        user_id = user.telegram_id
        lang = user.native_language or DEFAULT_LANGUAGE
        tier = UserTier(user.tier)
        limits = TIER_LIMITS[tier]

        # Check if session exists
        managed = self._sessions.get(user_id)

        need_new_session = managed is None

        if managed is not None:
            # Close session if turn or cost limit reached, and schedule summary.
            # Do NOT create a new session — the user's message was intended for
            # the old context and would confuse a fresh session.  Instead, the
            # summary includes a CTA button to continue.
            close_reason: CloseReason | None = None
            if managed.turn_count >= limits.max_turns_per_session:
                close_reason = CloseReason.TURN_LIMIT
            elif managed.accumulated_cost >= limits.max_cost_per_session_usd:
                close_reason = CloseReason.COST_LIMIT

            if close_reason is not None:
                session_data = _collect_session_data(managed)
                await self._close_session(user_id, reason=close_reason)
                if self._bot:
                    summary_task = asyncio.create_task(
                        _generate_and_send_summary(
                            self._bot, user_id, lang, session_data,
                            close_reason, managed,
                            managed.first_name, managed.user_streak,
                            managed.user_level, managed.target_language,
                        )
                    )
                    try:
                        await asyncio.wait_for(asyncio.shield(summary_task), timeout=20.0)
                    except (asyncio.TimeoutError, Exception):
                        summary_task.add_done_callback(_log_task_exception)
                return None  # Summary with CTA sent; caller should not respond

        if need_new_session:
            limit_error = await self._check_daily_limits(
                user_id, user.timezone or "UTC", lang, tier, limits,
            )
            if limit_error:
                return [limit_error]

            managed = await self._create_session(user, tier)
            if managed is None:
                return [t("session.busy", lang)]

        # Process message under session lock.
        # Re-check that the session is still active after acquiring the lock:
        # the cleanup loop could have closed it while we waited.
        # If the session was replaced, create a new one and retry so we always
        # hold the CORRECT session's lock during processing (not the old one).
        max_retries = 3
        for _attempt in range(max_retries):
            async with managed.lock:
                if self._sessions.get(user_id) is managed:
                    response_chunks = await self._process_message(managed, text, limits)
                    break
            # Session was closed while waiting — create a new one and retry
            managed = await self._create_session(user, tier)
            if managed is None:
                return [t("session.busy", lang)]
        else:
            return [t("session.busy", lang)]


        self._inject_limit_warnings(managed, limits, lang, response_chunks)
        return response_chunks

    async def _create_session(
        self,
        user: User,
        tier: UserTier,
    ) -> ManagedSession | None:
        """Create a new agent session for a user."""
        user_id = user.telegram_id
        limits = TIER_LIMITS[tier]

        # Acquire pool slot
        acquired = await session_pool.acquire_interactive()
        if not acquired:
            logger.warning("No available session slots for user {}", user_id)
            return None

        # Acquire Redis lock (returns owner token or None)
        lock_token = await acquire_session_lock(user_id, limits.redis_session_ttl_seconds)
        if lock_token is None:
            await session_pool.release_interactive()
            logger.warning("User {} already has an active session (Redis lock)", user_id)
            return None

        exit_stack = AsyncExitStack()
        try:
            # Reload user from DB to get fresh state. The `user` object from
            # the middleware may be stale when a session is closed and recreated
            # within the same handle_message call (turn/cost limit), because
            # the post-session pipeline updates sessions_completed and
            # last_session_at concurrently.
            async with async_session_factory() as db:
                fresh_user = await UserRepo.get(db, user_id)
            if fresh_user is not None:
                user = fresh_user

            session_type = SessionType.ONBOARDING if not user.onboarding_completed else SessionType.INTERACTIVE

            # Compute session context and system prompt
            session_ctx = compute_session_context(user)
            async with async_session_factory() as db:
                due_count = await VocabularyRepo.count_due(db, user_id)
                if user.sessions_completed > 0:
                    stale_topics, topic_performance = await _compute_stale_topics(db, user_id)
                else:
                    stale_topics, topic_performance = [], {}
                active_schedules = await ScheduleRepo.get_for_user(db, user_id)

                # Fetch active learning plan and derive progress
                active_plan = await LearningPlanRepo.get_active(db, user_id)
                plan_progress = None
                if active_plan:
                    all_plan_topics = [
                        t
                        for p in (active_plan.plan_data or {}).get("phases", [])
                        for t in p.get("topics", [])
                    ]
                    topic_stats = await ExerciseResultRepo.get_stats_for_topics(
                        db, user_id, all_plan_topics, active_plan.start_date,
                    )
                    plan_progress = compute_plan_progress(
                        active_plan.plan_data or {},
                        active_plan.total_weeks,
                        active_plan.start_date,
                        topic_stats,
                    )

                # Clear consumed celebrations so they aren't shown again
                needs_commit = False
                if session_ctx.get("celebrations"):
                    await UserRepo.clear_pending_celebrations(db, user_id)
                    needs_commit = True

                # Clear consumed notification context so it isn't shown
                # in future sessions (the prompt already captured it above).
                if session_ctx.get("notification_text"):
                    await UserRepo.update_fields(
                        db, user_id,
                        last_notification_text=None,
                        last_notification_at=None,
                    )
                    needs_commit = True

                if needs_commit:
                    await db.commit()

            system_prompt = build_system_prompt(
                user, session_ctx,
                session_type=session_type,
                due_count=due_count,
                stale_topics=stale_topics,
                topic_performance=topic_performance,
                active_schedules=[
                    {
                        "type": s.schedule_type,
                        "description": s.description,
                        "status": s.status,
                    }
                    for s in active_schedules
                ],
                active_plan=active_plan,
                plan_progress=plan_progress,
                has_web_search=web_search_available() and tier == UserTier.PREMIUM,
            )

            # Create DB session record
            db_session_id = uuid.uuid4()
            async with async_session_factory() as db:
                await SessionRepo.create(
                    db,
                    id=db_session_id,
                    user_id=user_id,
                    session_type=session_type,
                    system_prompt=system_prompt,
                )
                await db.commit()

            # Create per-session tools with closure-captured state
            all_tools, can_use_tool = create_session_tools(
                session_factory=async_session_factory,
                user_id=user_id,
                session_id=str(db_session_id),
                session_type=session_type,
                user_timezone=user.timezone or "UTC",
                user_tier=tier,
            )

            # Primary filter: exclude disallowed tools from the MCP server.
            # Since disallowed tools are excluded entirely, the SDK will never
            # offer them — no secondary can_use_tool callback is needed.
            tools = [tool for tool in all_tools if can_use_tool(tool.name)]
            allowed_tool_names = [
                f"mcp__langbot__{tool.name}" for tool in tools
            ]

            # Build per-session hooks with closure-captured state
            hooks, hook_state = build_session_hooks(user_id)

            # Seed hook state with user's recent scores for cross-session continuity
            seeded = list((user.recent_scores or [])[-tuning.hook_rolling_avg_window:])
            hook_state.exercise_scores = seeded
            hook_state._seeded_count = len(seeded)

            # Create MCP server with session-specific tools
            server = create_langbot_server(tools)

            # Build thinking config
            if limits.thinking_type == "disabled":
                thinking = {"type": "disabled"}
            else:
                thinking = {"type": "adaptive"}

            # Create SDK client
            options = ClaudeAgentOptions(
                model=limits.model,
                max_turns=limits.max_turns_per_session,
                thinking=thinking,
                effort=tuning.interactive_effort,
                mcp_servers={"langbot": server},
                allowed_tools=allowed_tool_names,
                permission_mode="bypassPermissions",
                system_prompt=system_prompt,
                hooks=hooks,
            )

            client = ClaudeSDKClient(options)
            await exit_stack.enter_async_context(client)

            managed = ManagedSession(
                client=client,
                user_id=user_id,
                db_session_id=db_session_id,
                tier=tier,
                hook_state=hook_state,
                native_language=user.native_language or DEFAULT_LANGUAGE,
                target_language=user.target_language or "",
                user_timezone=user.timezone or "UTC",
                first_name=user.first_name or "",
                user_level=user.level or "A1",
                user_streak=user.streak_days or 0,
                user_vocabulary_count=user.vocabulary_count or 0,
                session_type=session_type,
                lock_token=lock_token,
                exit_stack=exit_stack,
            )

            # Inform hooks about limits for wrap-up injection
            hook_state.max_turns = limits.max_turns_per_session
            hook_state.max_cost_usd = limits.max_cost_per_session_usd

            async with self._lock:
                self._sessions[user_id] = managed

            SESSIONS_CREATED.labels(tier=tier.value, session_type=session_type).inc()
            logger.info(
                "Session created for user {} (tier={}, model={})",
                user_id, tier, limits.model,
            )
            return managed

        except Exception:
            SESSION_ERRORS.labels(stage="create").inc()
            # AsyncExitStack closes only what was successfully entered (LIFO order)
            try:
                await exit_stack.aclose()
            except Exception:
                logger.warning("Failed to close resources during cleanup for user {}", user_id)
            await self._release_lock_and_pool(user_id, is_proactive=False, lock_token=lock_token)
            logger.exception("Failed to create session for user {}", user_id)
            return None

    async def _process_message(
        self,
        managed: ManagedSession,
        text: str,
        limits: TierLimits,
    ) -> list[str]:
        """Send a message to the agent and collect response."""
        response_chunks: list[str] = []
        managed.last_message_debug = None

        # Capture pre-message state for debug delta
        cost_before = managed.accumulated_cost
        tools_before = len(managed.tools_called)

        try:
            # Mark activity BEFORE the SDK call so the cleanup loop
            # doesn't consider this session idle while we're waiting
            # for the model response (which can take seconds).
            managed.last_activity_at = time.time()

            # Sync cost to hook state so UserPromptSubmit can inject
            # budget warnings before the agent starts its next turn.
            if managed.hook_state is not None:
                managed.hook_state.accumulated_cost = managed.accumulated_cost

            await managed.client.query(text)

            async for msg in managed.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_chunks.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            managed.tools_called.append(block.name)
                elif isinstance(msg, ResultMessage):
                    managed.accumulated_cost += msg.total_cost_usd or 0
                    managed.turn_count = (
                        msg.num_turns
                        if msg.num_turns is not None
                        else managed.turn_count + 1
                    )

            managed.last_activity_at = time.time()

            # Sync turn count to hook state for wrap-up injection
            if managed.hook_state is not None:
                managed.hook_state.turn_count = managed.turn_count

            # Record per-message metrics
            msg_cost = managed.accumulated_cost - cost_before
            MESSAGES_PROCESSED.labels(tier=managed.tier.value).inc()
            if msg_cost > 0:
                MESSAGE_COST_USD.labels(tier=managed.tier.value).observe(msg_cost)

            # Compute per-message debug info
            msg_tools = managed.tools_called[tools_before:]
            managed.last_message_debug = {
                "tools_called": [strip_mcp_prefix(t) for t in msg_tools],
                "tools_count": len(msg_tools),
                "message_cost": managed.accumulated_cost - cost_before,
                "accumulated_cost": managed.accumulated_cost,
                "turn_count": managed.turn_count,
                "turns_remaining": limits.max_turns_per_session - managed.turn_count,
                "tier": managed.tier.value,
                "model": limits.model,
                "session_duration_s": round(time.time() - managed.started_at, 1),
            }

            # Refresh Redis lock (ownership-verified: only extends if we still own it)
            still_owner = await refresh_session_lock(
                managed.user_id,
                limits.redis_session_ttl_seconds,
                token=managed.lock_token,
            )
            if not still_owner:
                logger.warning(
                    "Session lock lost for user {} — closing session",
                    managed.user_id,
                )
                try:
                    await self._close_session(managed.user_id, reason=CloseReason.ERROR)
                except Exception:
                    logger.warning("Failed to close lock-lost session for user {}", managed.user_id)

        except Exception:
            SESSION_ERRORS.labels(stage="process").inc()
            logger.exception("Error processing message for user {}", managed.user_id)
            response_chunks.append(t("session.error_retry", managed.native_language))
            # Close the broken session so the next message creates a fresh one
            # rather than repeatedly failing on a dead SDK client.
            try:
                await self._close_session(managed.user_id, reason=CloseReason.ERROR)
            except Exception:
                logger.warning("Failed to close broken session for user {}", managed.user_id)

        if not response_chunks:
            response_chunks.append(t("session.no_response", managed.native_language))

        return response_chunks

    async def _close_session(self, user_id: int, *, reason: str = CloseReason.UNKNOWN) -> None:
        """Close a session: remove from dict and release all resources."""
        managed = self._sessions.pop(user_id, None)
        if managed is None:
            return
        await self._release_session(user_id, managed, reason=reason)

    async def _release_session(
        self, user_id: int, managed: ManagedSession, *, reason: str = CloseReason.UNKNOWN,
    ) -> None:
        """Release all resources for a managed session (SDK client, DB, Redis, pool)."""
        duration = time.time() - managed.started_at
        SESSIONS_CLOSED.labels(tier=managed.tier.value, reason=reason).inc()
        SESSION_COST_USD.labels(tier=managed.tier.value, session_type=managed.session_type).observe(managed.accumulated_cost)
        SESSION_DURATION_SECONDS.labels(tier=managed.tier.value).observe(duration)

        logger.info(
            "Closing session for user {} (reason={}, turns={}, cost=${:.4f})",
            user_id, reason, managed.turn_count, managed.accumulated_cost,
        )

        # Close SDK client by terminating the subprocess directly.
        # We cannot use exit_stack.aclose() → client.__aexit__() because
        # the SDK's internal anyio TaskGroup must be exited from the same
        # asyncio task that entered it.  Sessions are created in one aiogram
        # handler task but closed from another (cost/turn limit, cleanup
        # loop, shutdown), so we bypass the TaskGroup and close the
        # transport (subprocess) directly.
        try:
            await asyncio.wait_for(
                _force_close_sdk_client(managed.client),
                timeout=tuning.sdk_close_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("Resource cleanup timed out for user {} — killing subprocess", user_id)
            _kill_sdk_subprocess(managed.client)
        except Exception:
            logger.exception("Error during resource cleanup for user {}", user_id)
            _kill_sdk_subprocess(managed.client)

        await self._release_lock_and_pool(user_id, managed.is_proactive, lock_token=managed.lock_token)

        # Update session record in DB
        try:
            async with async_session_factory() as db:
                await SessionRepo.update_end(
                    db,
                    managed.db_session_id,
                    cost_usd=managed.accumulated_cost,
                    num_turns=managed.turn_count,
                    tool_calls_count=len(managed.tools_called),
                    tool_calls_detail={"calls": [
                        {"tool": tc} for tc in managed.tools_called
                    ]} if managed.tools_called else None,
                    duration_ms=int((time.time() - managed.started_at) * 1000),
                )
                await db.commit()
        except SQLAlchemyError:
            logger.exception("Error updating session record for user {}", user_id)

        # Fire post-session pipeline as background task with 1 retry.
        # On first failure, wait 2s then retry once. Exceptions are logged
        # via done callback so silent data loss is visible.
        #
        # For automated closes (idle/turn/cost/shutdown), suppress immediate
        # celebration messages — they arrive as separate Telegram messages that
        # interleave with the session summary and create confusing UX.
        # Celebrations stay in pending_celebrations for next session start.
        # Only explicit close (/end) sends celebrations immediately.
        celebration_bot = (
            self._bot if reason == CloseReason.EXPLICIT_CLOSE else None
        )

        async def _post_session_with_retry() -> None:
            for attempt in range(2):
                try:
                    await asyncio.wait_for(
                        run_post_session(
                            user_id=user_id,
                            session_id=managed.db_session_id,
                            tools_called=managed.tools_called,
                            close_reason=reason,
                            bot=celebration_bot,
                            agent_stopped=bool(managed.hook_state and managed.hook_state.stop_data),
                            turn_count=managed.turn_count,
                        ),
                        timeout=tuning.post_session_timeout_seconds,
                    )
                    return  # Success
                except Exception:
                    if attempt == 0:
                        logger.warning("Post-session pipeline failed for user {} (attempt 1), retrying in 2s", user_id)
                        await asyncio.sleep(2)
                    else:
                        raise  # Re-raise on final attempt for done callback

        try:
            task = asyncio.create_task(_post_session_with_retry())
            task.add_done_callback(_log_task_exception)
        except Exception:
            logger.exception("Error launching post-session pipeline for user {}", user_id)

    async def _cleanup_loop(self) -> None:
        """Periodically close idle sessions and warn before timeout."""
        while True:
            try:
                await asyncio.sleep(tuning.cleanup_interval_seconds)
                now = time.time()

                # Pop idle sessions under the lock, then release resources
                # outside to avoid blocking new session creation.
                to_close: list[tuple[int, ManagedSession]] = []
                to_warn: list[tuple[int, int]] = []  # (user_id, remaining_seconds)
                async with self._lock:
                    for user_id, managed in list(self._sessions.items()):
                        idle_timeout = TIER_LIMITS[managed.tier].session_idle_timeout_seconds
                        idle_seconds = now - managed.last_activity_at
                        if idle_seconds > idle_timeout:
                            to_close.append((user_id, self._sessions.pop(user_id)))
                        elif (
                            not managed.idle_warned
                            and not managed.is_proactive
                            and idle_seconds > idle_timeout * tuning.idle_warn_fraction
                        ):
                            managed.idle_warned = True
                            remaining = int(idle_timeout - idle_seconds)
                            to_warn.append((user_id, remaining))

                # Send idle warnings in parallel (best-effort, don't block cleanup)
                if self._bot and to_warn:
                    async def _send_idle_warn(uid: int, remaining: int) -> None:
                        try:
                            managed_w = self._sessions.get(uid)
                            warn_lang = managed_w.native_language if managed_w else DEFAULT_LANGUAGE
                            minutes = max(1, round(remaining / 60))
                            await self._bot.send_message(
                                uid,
                                t("session.idle_warn", warn_lang, minutes=minutes),
                            )
                        except Exception:
                            logger.debug("Failed to send idle warning to user {}", uid)

                    await asyncio.gather(
                        *(_send_idle_warn(uid, rem) for uid, rem in to_warn),
                        return_exceptions=True,
                    )

                for user_id, managed in to_close:
                    # Collect session data before releasing resources (data survives SDK close)
                    if self._bot and not managed.is_proactive:
                        session_data = _collect_session_data(managed)
                        task = asyncio.create_task(
                            _generate_and_send_summary(
                                self._bot, user_id, managed.native_language,
                                session_data, CloseReason.IDLE_TIMEOUT, managed,
                                managed.first_name, managed.user_streak,
                                managed.user_level, managed.target_language,
                                skip_if_active_fn=lambda _u=user_id: _u in self._sessions,
                            )
                        )
                        task.add_done_callback(_log_task_exception)
                    await self._release_session(user_id, managed, reason=CloseReason.IDLE_TIMEOUT)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in cleanup loop")

    def get_active_count(self) -> int:
        return len(self._sessions)

    def has_active_session(self, user_id: int) -> bool:
        return user_id in self._sessions

    def get_debug_info(self, user_id: int) -> dict | None:
        """Get debug info from the last message processed for a user."""
        managed = self._sessions.get(user_id)
        if managed is None or managed.last_message_debug is None:
            return None
        debug = dict(managed.last_message_debug)
        debug["active_sessions_global"] = len(self._sessions)
        return debug

    async def close_user_session(self, user_id: int) -> ManagedSession | None:
        """Explicitly close a user's session, waiting for any in-progress message.

        Returns the closed ``ManagedSession`` so callers can access session data
        for summary generation.  Returns ``None`` if no session was active.
        """
        managed = self._sessions.get(user_id)
        if managed is None:
            return None
        # Wait for any in-progress message to finish before closing
        async with managed.lock:
            # Re-check: the session might have been closed by another path
            # (e.g. cleanup loop) while we waited for the lock
            if self._sessions.get(user_id) is managed:
                await self._close_session(user_id, reason=CloseReason.EXPLICIT_CLOSE)
                return managed
        return None


# Global instance
session_manager = SessionManager()
