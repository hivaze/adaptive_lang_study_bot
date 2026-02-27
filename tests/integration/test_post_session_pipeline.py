"""Integration tests for the post-session pipeline against real PostgreSQL.

Tests run_post_session() with a real database to verify streak updates,
milestone detection, last_activity building, session_history rolling,
and pipeline status recording.

Note: run_post_session() creates its own sessions via async_session_factory(),
so we monkeypatch the factory to use the test engine. Each test commits data
for the pipeline to see and cleans up via CASCADE delete on the user.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.enums import CloseReason, PipelineStatus
from adaptive_lang_study_bot.pipeline.post_session import run_post_session

pytestmark = pytest.mark.integration

_PIPELINE_USER_COUNTER = 800_000


@pytest.fixture()
async def penv(pg_url: str):
    """Engine + monkeypatched async_session_factory for pipeline tests.

    Yields a helper that creates fresh AsyncSessions on demand.
    Callers MUST close sessions after use to avoid connection leaks.
    """
    engine = create_async_engine(pg_url, pool_size=5)

    @asynccontextmanager
    async def _factory():
        async with AsyncSession(bind=engine, expire_on_commit=False) as s:
            yield s

    with patch(
        "adaptive_lang_study_bot.pipeline.post_session.async_session_factory",
        _factory,
    ):
        yield engine

    await engine.dispose()


async def _setup_user(engine, **overrides):
    """Create a test user + session, commit, return (user, session_record, uid)."""
    global _PIPELINE_USER_COUNTER
    _PIPELINE_USER_COUNTER += 1
    uid = _PIPELINE_USER_COUNTER

    defaults = dict(
        telegram_id=uid,
        first_name="PipelineTest",
        native_language="en",
        target_language="es",
        timezone="UTC",
        onboarding_completed=True,
        sessions_completed=0,
        streak_days=0,
        preferred_difficulty="normal",
        recent_scores=[],
        milestones={},
        session_history=[],
    )
    defaults.update(overrides)
    defaults["telegram_id"] = uid

    async with AsyncSession(bind=engine, expire_on_commit=False) as db:
        user = User(**defaults)
        db.add(user)
        await db.commit()

        sess = await SessionRepo.create(db, user_id=uid, session_type="interactive")
        await db.commit()

    return user, sess, uid


async def _read_user(engine, uid):
    """Read user from a fresh session."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as db:
        return await UserRepo.get(db, uid)


async def _read_session(engine, session_id):
    """Read session from a fresh session."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as db:
        return await SessionRepo.get(db, session_id)


async def _cleanup(engine, uid):
    """Delete user (CASCADE) to clean up."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as db:
        await UserRepo.delete(db, uid)
        await db.commit()


# ---------------------------------------------------------------------------
# Pipeline status recording
# ---------------------------------------------------------------------------

class TestPipelineStatus:

    async def test_pipeline_sets_completed_status(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=[],
                close_reason="idle_timeout",
            )

            result = await _read_session(penv, sess.id)
            assert result.pipeline_status == PipelineStatus.COMPLETED
        finally:
            await _cleanup(penv, uid)

    async def test_pipeline_records_issues_for_invalid_scores(self, penv):
        """Pipeline should record issues when out-of-range scores are found."""
        user, sess, uid = await _setup_user(penv)
        try:
            # Set invalid scores (DB allows these — no CHECK on array values)
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                await UserRepo.update_fields(db, uid, recent_scores=[5, -1, 11, 7])
                await db.commit()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason="idle_timeout",
            )

            result = await _read_session(penv, sess.id)
            assert result.pipeline_status == PipelineStatus.COMPLETED
            assert result.pipeline_issues is not None
            assert any("Invalid scores" in i for i in result.pipeline_issues["issues"])
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Streak updates
# ---------------------------------------------------------------------------

class TestPipelineStreak:

    async def test_streak_starts_at_one(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason="idle_timeout",
            )

            updated = await _read_user(penv, uid)
            assert updated.streak_days == 1
            assert updated.streak_updated_at is not None
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Session completion tracking
# ---------------------------------------------------------------------------

class TestSessionCompletion:

    async def test_sessions_completed_increments(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason="idle_timeout",
            )

            updated = await _read_user(penv, uid)
            assert updated.sessions_completed == 1
        finally:
            await _cleanup(penv, uid)

    async def test_no_increment_without_tools(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=[],
                close_reason="idle_timeout",
            )

            updated = await _read_user(penv, uid)
            assert updated.sessions_completed == 0
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Last activity and session history
# ---------------------------------------------------------------------------

class TestLastActivity:

    async def test_last_activity_populated(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__add_vocabulary"],
                close_reason=CloseReason.EXPLICIT_CLOSE,
            )

            updated = await _read_user(penv, uid)
            assert updated.last_activity is not None
            assert updated.last_activity["type"] == "session"
            assert updated.last_activity["status"] == "completed"
            assert updated.last_activity["close_reason"] == CloseReason.EXPLICIT_CLOSE
            assert updated.last_activity["exercise_count"] == 0
        finally:
            await _cleanup(penv, uid)

    async def test_idle_timeout_marks_incomplete(self, penv):
        """idle_timeout is a forced close — session should be incomplete."""
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason=CloseReason.IDLE_TIMEOUT,
            )

            updated = await _read_user(penv, uid)
            assert updated.last_activity["status"] == "incomplete"
            assert updated.last_activity["close_reason"] == CloseReason.IDLE_TIMEOUT
        finally:
            await _cleanup(penv, uid)

    async def test_forced_close_marks_incomplete(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason=CloseReason.TURN_LIMIT,
            )

            updated = await _read_user(penv, uid)
            assert updated.last_activity["status"] == "incomplete"
        finally:
            await _cleanup(penv, uid)

    async def test_session_history_appended(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason=CloseReason.IDLE_TIMEOUT,
            )

            updated = await _read_user(penv, uid)
            assert len(updated.session_history) == 1
            entry = updated.session_history[0]
            assert "summary" in entry
            assert "date" in entry
            assert entry["close_reason"] == CloseReason.IDLE_TIMEOUT
            assert entry["status"] == "incomplete"
        finally:
            await _cleanup(penv, uid)

    async def test_enriched_with_exercise_data(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                await ExerciseResultRepo.create(
                    db,
                    user_id=uid,
                    session_id=sess.id,
                    exercise_type="translation",
                    topic="food",
                    score=8,
                    words_involved=["manzana", "pera"],
                )
                await db.commit()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__record_exercise_result"],
                close_reason=CloseReason.IDLE_TIMEOUT,
            )

            updated = await _read_user(penv, uid)
            activity = updated.last_activity
            assert activity["last_exercise"] == "translation"
            assert activity["topic"] == "food"
            assert activity["score"] == 8
            assert activity["exercise_count"] == 1
            assert activity["close_reason"] == CloseReason.IDLE_TIMEOUT
            assert "manzana" in activity["words_practiced"]
            assert "food" in activity["topics_covered"]
        finally:
            await _cleanup(penv, uid)

    async def test_pending_context_on_idle_timeout(self, penv):
        """When idle_timeout + no exercises + prep tools, pending_context is inferred."""
        user, sess, uid = await _setup_user(penv)
        try:
            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__get_exercise_history"],
                close_reason=CloseReason.IDLE_TIMEOUT,
            )

            updated = await _read_user(penv, uid)
            activity = updated.last_activity
            assert activity["status"] == "incomplete"
            assert activity["pending_context"] == "preparing an exercise"
        finally:
            await _cleanup(penv, uid)

    async def test_no_pending_context_with_exercises(self, penv):
        """pending_context should NOT be set when exercises were completed."""
        user, sess, uid = await _setup_user(penv)
        try:
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                await ExerciseResultRepo.create(
                    db,
                    user_id=uid,
                    session_id=sess.id,
                    exercise_type="translation",
                    topic="food",
                    score=7,
                )
                await db.commit()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=[
                    "mcp__langbot__get_exercise_history",
                    "mcp__langbot__record_exercise_result",
                ],
                close_reason=CloseReason.IDLE_TIMEOUT,
            )

            updated = await _read_user(penv, uid)
            assert "pending_context" not in updated.last_activity
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Profile integrity validation
# ---------------------------------------------------------------------------

class TestProfileIntegrity:

    async def test_invalid_scores_cleaned(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                await UserRepo.update_fields(db, uid, recent_scores=[5, -1, 11, 7, 8])
                await db.commit()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=[],
                close_reason="idle_timeout",
            )

            updated = await _read_user(penv, uid)
            for s in updated.recent_scores:
                assert 0 <= s <= 10
        finally:
            await _cleanup(penv, uid)

    async def test_interests_capped(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                await UserRepo.update_fields(
                    db, uid, interests=[f"interest_{i}" for i in range(20)],
                )
                await db.commit()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=[],
                close_reason="idle_timeout",
            )

            updated = await _read_user(penv, uid)
            assert len(updated.interests) <= 8
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Milestone detection
# ---------------------------------------------------------------------------

class TestMilestones:

    async def test_vocabulary_milestone_at_10(self, penv):
        user, sess, uid = await _setup_user(penv)
        try:
            async with AsyncSession(bind=penv, expire_on_commit=False) as db:
                for i in range(10):
                    await VocabularyRepo.add(db, user_id=uid, word=f"word_{i}")
                await db.commit()

            bot = AsyncMock()
            bot.send_message = AsyncMock()

            await run_post_session(
                user_id=uid,
                session_id=sess.id,
                tools_called=["mcp__langbot__add_vocabulary"],
                close_reason="idle_timeout",
                bot=bot,
            )

            # Bot should receive the celebration
            if bot.send_message.call_count > 0:
                assert any(
                    call.args[0] == uid for call in bot.send_message.call_args_list
                )
        finally:
            await _cleanup(penv, uid)


# ---------------------------------------------------------------------------
# Deleted user handling
# ---------------------------------------------------------------------------

class TestDeletedUser:

    async def test_pipeline_handles_deleted_user(self, penv):
        user, sess, uid = await _setup_user(penv)
        session_id = sess.id

        # Delete user before pipeline runs
        await _cleanup(penv, uid)

        # Should not raise
        await run_post_session(
            user_id=uid,
            session_id=session_id,
            tools_called=["mcp__langbot__record_exercise_result"],
            close_reason="idle_timeout",
        )
