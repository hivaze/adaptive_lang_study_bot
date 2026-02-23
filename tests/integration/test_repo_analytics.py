"""Integration tests for analytics and batch repository methods.

Covers VocabularyRepo.count_due_batch, get_state_counts,
ExerciseResultRepo.get_score_summary, get_topic_stats,
SessionRepo.get_activity_stats — all previously untested.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    SessionRepo,
    VocabularyRepo,
)

pytestmark = pytest.mark.integration

_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# VocabularyRepo batch and analytics
# ---------------------------------------------------------------------------

class TestCountDueBatch:

    async def test_batch_returns_all_user_ids(self, db_session: AsyncSession, make_user):
        """count_due_batch should return counts for ALL requested user_ids, even zero."""
        u1 = await make_user()
        u2 = await make_user()
        u3 = await make_user()

        # u1 has 2 overdue cards
        await VocabularyRepo.add(
            db_session, user_id=u1.telegram_id, word="a1",
            fsrs_due=_NOW - timedelta(hours=1),
        )
        await VocabularyRepo.add(
            db_session, user_id=u1.telegram_id, word="a2",
            fsrs_due=_NOW - timedelta(hours=2),
        )

        # u2 has 1 overdue + 1 future
        await VocabularyRepo.add(
            db_session, user_id=u2.telegram_id, word="b1",
            fsrs_due=_NOW - timedelta(hours=1),
        )
        await VocabularyRepo.add(
            db_session, user_id=u2.telegram_id, word="b2",
            fsrs_due=_NOW + timedelta(days=7),
        )

        # u3 has no vocabulary at all

        user_ids = [u1.telegram_id, u2.telegram_id, u3.telegram_id]
        counts = await VocabularyRepo.count_due_batch(db_session, user_ids)

        assert counts[u1.telegram_id] == 2
        assert counts[u2.telegram_id] == 1
        assert counts[u3.telegram_id] == 0

    async def test_batch_empty_input(self, db_session: AsyncSession):
        """Empty user_ids list should return empty dict."""
        result = await VocabularyRepo.count_due_batch(db_session, [])
        assert result == {}


class TestGetStateCounts:

    async def test_counts_by_fsrs_state(self, db_session: AsyncSession, make_user):
        """get_state_counts should return correct counts per FSRS state."""
        user = await make_user()

        # 2 New (state=0), 1 Learning (state=1), 1 Review (state=2)
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="new1", fsrs_state=0,
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="new2", fsrs_state=0,
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="learning", fsrs_state=1,
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="review", fsrs_state=2,
        )

        counts = await VocabularyRepo.get_state_counts(db_session, user.telegram_id)
        assert counts[0] == 2  # New
        assert counts[1] == 1  # Learning
        assert counts[2] == 1  # Review

    async def test_empty_vocabulary(self, db_session: AsyncSession, make_user):
        """User with no vocabulary should return empty dict."""
        user = await make_user()
        counts = await VocabularyRepo.get_state_counts(db_session, user.telegram_id)
        assert counts == {}


# ---------------------------------------------------------------------------
# ExerciseResultRepo analytics
# ---------------------------------------------------------------------------

class TestGetScoreSummary:

    async def test_summary_with_data(self, db_session: AsyncSession, make_user):
        """get_score_summary should compute correct aggregate stats."""
        user = await make_user()
        session = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )

        for score in [3, 5, 7, 9, 10]:
            await ExerciseResultRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_id=session.id,
                exercise_type="translation",
                topic="food",
                score=score,
            )

        summary = await ExerciseResultRepo.get_score_summary(
            db_session, user.telegram_id, days=30,
        )
        assert summary["count"] == 5
        assert summary["min"] == 3
        assert summary["max"] == 10
        assert 6.0 <= summary["avg"] <= 7.0  # avg of 3,5,7,9,10 = 6.8

    async def test_summary_no_data(self, db_session: AsyncSession, make_user):
        """No exercises should return count=0 and None for stats."""
        user = await make_user()
        summary = await ExerciseResultRepo.get_score_summary(
            db_session, user.telegram_id, days=30,
        )
        assert summary["count"] == 0
        assert summary["avg"] is None


class TestGetTopicStats:

    async def test_per_topic_stats(self, db_session: AsyncSession, make_user):
        """get_topic_stats should group by topic and compute per-topic averages."""
        user = await make_user()
        session = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )

        # Food: 3 exercises, scores 5, 7, 8 (avg=6.7)
        for score in [5, 7, 8]:
            await ExerciseResultRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_id=session.id,
                exercise_type="translation",
                topic="food",
                score=score,
            )

        # Colors: 2 exercises, scores 9, 10 (avg=9.5)
        for score in [9, 10]:
            await ExerciseResultRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_id=session.id,
                exercise_type="fill_blank",
                topic="colors",
                score=score,
            )

        stats = await ExerciseResultRepo.get_topic_stats(
            db_session, user.telegram_id, days=30,
        )
        assert len(stats) == 2
        food = next(s for s in stats if s["topic"] == "food")
        colors = next(s for s in stats if s["topic"] == "colors")

        assert food["exercise_count"] == 3
        assert colors["exercise_count"] == 2
        assert food["avg_score"] is not None
        assert colors["avg_score"] is not None
        assert colors["avg_score"] > food["avg_score"]

    async def test_ordered_by_exercise_count(self, db_session: AsyncSession, make_user):
        """Topics should be ordered by exercise count (desc)."""
        user = await make_user()
        session = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )

        # 3 food, 1 color
        for _ in range(3):
            await ExerciseResultRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_id=session.id,
                exercise_type="translation",
                topic="food",
                score=7,
            )
        await ExerciseResultRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_id=session.id,
            exercise_type="translation",
            topic="colors",
            score=8,
        )

        stats = await ExerciseResultRepo.get_topic_stats(
            db_session, user.telegram_id, days=30,
        )
        # Food should come first (more exercises)
        assert stats[0]["topic"] == "food"


# ---------------------------------------------------------------------------
# SessionRepo analytics
# ---------------------------------------------------------------------------

class TestGetActivityStats:

    async def test_activity_stats(self, db_session: AsyncSession, make_user):
        """get_activity_stats should return session count and avg duration."""
        user = await make_user()
        for dur_ms in [60_000, 120_000, 180_000]:
            sess = await SessionRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_type="interactive",
            )
            await SessionRepo.update_end(
                db_session, sess.id, duration_ms=dur_ms,
            )

        # Also create a proactive session (should NOT be counted)
        await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="proactive_nudge",
        )

        stats = await SessionRepo.get_activity_stats(
            db_session, user.telegram_id, days=7,
        )
        assert stats["session_count"] == 3
        assert stats["avg_duration_ms"] == 120_000  # avg of 60k, 120k, 180k

    async def test_activity_stats_no_sessions(self, db_session: AsyncSession, make_user):
        """No sessions should return count=0 and None avg."""
        user = await make_user()
        stats = await SessionRepo.get_activity_stats(
            db_session, user.telegram_id, days=7,
        )
        assert stats["session_count"] == 0
        assert stats["avg_duration_ms"] is None


class TestCostPerUser:

    async def test_cost_breakdown(self, db_session: AsyncSession, make_user):
        """get_cost_per_user should aggregate costs per user."""
        u1 = await make_user()
        u2 = await make_user()

        s1 = await SessionRepo.create(
            db_session, user_id=u1.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s1.id, cost_usd=0.15)

        s2 = await SessionRepo.create(
            db_session, user_id=u1.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s2.id, cost_usd=0.25)

        s3 = await SessionRepo.create(
            db_session, user_id=u2.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s3.id, cost_usd=0.50)

        results = await SessionRepo.get_cost_per_user(db_session, days=7)
        assert len(results) >= 2

        # u2 should be first (higher total cost)
        user_costs = {r[0]: r[2] for r in results}
        assert user_costs[u2.telegram_id] == pytest.approx(0.50, abs=0.01)
        assert user_costs[u1.telegram_id] == pytest.approx(0.40, abs=0.01)


# ---------------------------------------------------------------------------
# SessionRepo.count_since
# ---------------------------------------------------------------------------

class TestCountSince:

    async def test_count_since(self, db_session: AsyncSession, make_user):
        """count_since should count interactive sessions after the cutoff."""
        user = await make_user()
        cutoff = _NOW - timedelta(days=7)

        # 2 interactive sessions after cutoff
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )

        count = await SessionRepo.count_since(db_session, user.telegram_id, cutoff)
        assert count == 2

    async def test_count_since_excludes_proactive(self, db_session: AsyncSession, make_user):
        """Proactive sessions should NOT be counted."""
        user = await make_user()
        cutoff = _NOW - timedelta(days=7)

        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="proactive_nudge",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="proactive_review",
        )

        count = await SessionRepo.count_since(db_session, user.telegram_id, cutoff)
        assert count == 1

    async def test_count_since_no_sessions(self, db_session: AsyncSession, make_user):
        """No sessions should return 0."""
        user = await make_user()
        cutoff = _NOW - timedelta(days=7)
        count = await SessionRepo.count_since(db_session, user.telegram_id, cutoff)
        assert count == 0
