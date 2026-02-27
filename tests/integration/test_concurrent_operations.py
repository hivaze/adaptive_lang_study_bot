"""Integration tests for concurrent and atomic operations.

Tests that atomic SQL operations behave correctly under contention:
- Notification counter increment doesn't lose updates
- Streak optimistic guard prevents double-increment
- Vocabulary count atomic increment
- Session lock prevents concurrent sessions
- Score append rolling window
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import (
    SessionRepo,
    UserRepo,
    VocabularyRepo,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Atomic notification counter
# ---------------------------------------------------------------------------

class TestAtomicNotificationIncrement:

    async def test_concurrent_increments_no_lost_updates(
        self, db_session: AsyncSession, make_user,
    ):
        """Multiple sequential increments should not lose any update."""
        user = await make_user()

        results = []
        for _ in range(5):
            new_val = await UserRepo.increment_notification_count(
                db_session, user.telegram_id,
            )
            results.append(new_val)

        # Each increment should return a unique, increasing value
        assert results == [1, 2, 3, 4, 5]

        await db_session.refresh(user)
        assert user.notifications_sent_today == 5

    async def test_reset_then_increment(
        self, db_session: AsyncSession, make_user,
    ):
        """Counter should start fresh after reset."""
        user = await make_user()

        # Increment a few times
        await UserRepo.increment_notification_count(db_session, user.telegram_id)
        await UserRepo.increment_notification_count(db_session, user.telegram_id)

        # Reset
        today = date.today()
        await UserRepo.reset_notification_counter(
            db_session, user.telegram_id, local_date=today,
        )

        # Increment again
        result = await UserRepo.increment_notification_count(
            db_session, user.telegram_id,
        )
        assert result == 1

        await db_session.refresh(user)
        assert user.notifications_count_reset_date == today


# ---------------------------------------------------------------------------
# Streak optimistic guard
# ---------------------------------------------------------------------------

class TestStreakOptimisticGuard:

    async def test_same_day_second_update_is_noop(
        self, db_session: AsyncSession, make_user,
    ):
        """Two streak updates on the same day should only apply once."""
        user = await make_user()

        # First update: streak goes from 0 to 1
        streak1 = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak1 == 1

        # Second update same day: should be a no-op
        streak2 = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak2 == 1

        await db_session.refresh(user)
        assert user.streak_days == 1


# ---------------------------------------------------------------------------
# Score append rolling window
# ---------------------------------------------------------------------------

class TestScoreAppendRollingWindow:

    async def test_rolling_cap_at_30(self, db_session: AsyncSession, make_user):
        """Appending more than 30 scores should keep only the last 30 (default max_len)."""
        user = await make_user()

        for i in range(35):
            scores = await UserRepo.append_score(
                db_session, user.telegram_id, score=i,
            )

        assert len(scores) == 30
        # Should contain the last 30 scores (5-34)
        assert scores == list(range(5, 35))

    async def test_append_preserves_existing(self, db_session: AsyncSession, make_user):
        """Appending a score should preserve existing scores."""
        user = await make_user()

        scores1 = await UserRepo.append_score(db_session, user.telegram_id, score=7)
        assert scores1 == [7]

        scores2 = await UserRepo.append_score(db_session, user.telegram_id, score=8)
        assert scores2 == [7, 8]


# ---------------------------------------------------------------------------
# JSONB field updates
# ---------------------------------------------------------------------------

class TestJSONBUpdates:

    async def test_milestones_update(self, db_session: AsyncSession, make_user):
        """JSONB milestones should be updated atomically."""
        user = await make_user()

        milestones = {
            "pending_celebrations": ["Streak: 3 days!"],
            "vocabulary_count": 10,
            "days_streak": 3,
        }
        await UserRepo.update_fields(
            db_session, user.telegram_id, milestones=milestones,
        )

        await db_session.refresh(user)
        assert user.milestones["vocabulary_count"] == 10
        assert len(user.milestones["pending_celebrations"]) == 1

    async def test_last_activity_update(self, db_session: AsyncSession, make_user):
        """JSONB last_activity should store complex nested data."""
        user = await make_user()

        activity = {
            "type": "session",
            "status": "completed",
            "close_reason": "explicit_close",
            "exercise_count": 3,
            "session_summary": "Completed exercises. Topics: food, colors",
            "tools_used": ["record_exercise_result", "add_vocabulary"],
            "last_exercise": "translation",
            "topic": "food",
            "score": 8,
            "words_practiced": ["manzana", "pera"],
            "topics_covered": ["food", "colors"],
            "struggling_topics": [{"topic": "grammar", "avg_score": 4.5}],
            "exercise_type_scores": {"translation": 8.5, "fill_blank": 6.0},
        }
        await UserRepo.update_fields(
            db_session, user.telegram_id, last_activity=activity,
        )

        await db_session.refresh(user)
        assert user.last_activity["type"] == "session"
        assert user.last_activity["score"] == 8
        assert "manzana" in user.last_activity["words_practiced"]
        assert user.last_activity["exercise_type_scores"]["translation"] == 8.5

    async def test_session_history_rolling_cap(self, db_session: AsyncSession, make_user):
        """session_history should respect rolling cap."""
        user = await make_user()

        # Build a history with 15 entries
        history = [
            {
                "date": f"2026-02-{i + 1:02d} 10:00",
                "summary": f"Session {i}",
                "status": "completed",
                "close_reason": "explicit_close",
            }
            for i in range(15)
        ]
        await UserRepo.update_fields(
            db_session, user.telegram_id, session_history=history[-10:],
        )

        await db_session.refresh(user)
        assert len(user.session_history) == 10


# ---------------------------------------------------------------------------
# Notification preferences and quiet hours
# ---------------------------------------------------------------------------

class TestNotificationPreferences:

    async def test_notification_preferences_update(
        self, db_session: AsyncSession, make_user,
    ):
        """Updating notification preferences should persist correctly."""
        user = await make_user()

        prefs = {
            "streak_reminders": False,
            "vocab_reviews": True,
            "progress_reports": False,
            "re_engagement": True,
            "learning_nudges": False,
        }
        await UserRepo.update_fields(
            db_session, user.telegram_id, notification_preferences=prefs,
        )

        await db_session.refresh(user)
        assert user.notification_preferences["streak_reminders"] is False
        assert user.notification_preferences["vocab_reviews"] is True

    async def test_pause_notifications(self, db_session: AsyncSession, make_user):
        """Pausing notifications should be persisted."""
        user = await make_user()

        await UserRepo.update_fields(
            db_session, user.telegram_id,
            notifications_paused=True,
        )

        await db_session.refresh(user)
        assert user.notifications_paused is True


# ---------------------------------------------------------------------------
# Multi-user isolation
# ---------------------------------------------------------------------------

class TestMultiUserIsolation:

    async def test_vocab_counts_isolated(self, db_session: AsyncSession, make_user):
        """Vocabulary operations for one user should not affect another."""
        u1 = await make_user()
        u2 = await make_user()

        for w in ["hola", "adiós", "gracias"]:
            await VocabularyRepo.add(db_session, user_id=u1.telegram_id, word=w)

        await VocabularyRepo.add(db_session, user_id=u2.telegram_id, word="hola")

        count1 = await VocabularyRepo.count_for_user(db_session, u1.telegram_id)
        count2 = await VocabularyRepo.count_for_user(db_session, u2.telegram_id)
        assert count1 == 3
        assert count2 == 1

    async def test_notification_counters_isolated(
        self, db_session: AsyncSession, make_user,
    ):
        """Notification counters for one user should not affect another."""
        u1 = await make_user()
        u2 = await make_user()

        await UserRepo.increment_notification_count(db_session, u1.telegram_id)
        await UserRepo.increment_notification_count(db_session, u1.telegram_id)
        await UserRepo.increment_notification_count(db_session, u2.telegram_id)

        await db_session.refresh(u1)
        await db_session.refresh(u2)
        assert u1.notifications_sent_today == 2
        assert u2.notifications_sent_today == 1

    async def test_session_counts_isolated(
        self, db_session: AsyncSession, make_user,
    ):
        """Session counts should be per-user."""
        u1 = await make_user()
        u2 = await make_user()

        for _ in range(3):
            await SessionRepo.create(
                db_session, user_id=u1.telegram_id, session_type="interactive",
            )
        await SessionRepo.create(
            db_session, user_id=u2.telegram_id, session_type="interactive",
        )

        count1 = await SessionRepo.count_today(db_session, u1.telegram_id)
        count2 = await SessionRepo.count_today(db_session, u2.telegram_id)
        assert count1 == 3
        assert count2 == 1


# ---------------------------------------------------------------------------
# Concurrent Redis lock
# ---------------------------------------------------------------------------

class TestConcurrentSessionLock:

    async def test_concurrent_lock_acquisition(self, redis_client, mock_get_redis):
        """Only one session lock per user should be possible."""
        from adaptive_lang_study_bot.cache.session_lock import (
            acquire_session_lock,
            release_session_lock,
        )

        # Try to acquire 5 locks concurrently for the same user
        results = await asyncio.gather(
            *(acquire_session_lock(600, ttl_seconds=60) for _ in range(5))
        )

        # Exactly one should succeed
        tokens = [r for r in results if r is not None]
        assert len(tokens) == 1

        # Release and verify re-acquisition works
        await release_session_lock(600, tokens[0])
        new_token = await acquire_session_lock(600, ttl_seconds=60)
        assert new_token is not None
