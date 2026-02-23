"""Integration tests for ExerciseResultRepo, NotificationRepo, VocabularyReviewLogRepo."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    NotificationRepo,
    SessionRepo,
    VocabularyRepo,
    VocabularyReviewLogRepo,
)

pytestmark = pytest.mark.integration


# ===========================================================================
# ExerciseResultRepo
# ===========================================================================

class TestExerciseResultCRUD:

    async def test_create_and_get_recent(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session,
            user_id=user.telegram_id,
            exercise_type="fill_blank",
            topic="grammar",
            score=8,
        )
        await ExerciseResultRepo.create(
            db_session,
            user_id=user.telegram_id,
            exercise_type="translation",
            topic="vocabulary",
            score=6,
        )

        recent = await ExerciseResultRepo.get_recent(db_session, user.telegram_id)
        assert len(recent) == 2
        # Most recent first
        assert recent[0].exercise_type == "translation"

    async def test_get_recent_topic_filter(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="grammar", score=7,
        )
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="vocabulary", score=9,
        )

        grammar = await ExerciseResultRepo.get_recent(
            db_session, user.telegram_id, topic="grammar",
        )
        assert len(grammar) == 1
        assert grammar[0].topic == "grammar"

    async def test_get_by_session(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await ExerciseResultRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_id=sess.id,
            exercise_type="translation",
            topic="food",
            score=10,
        )
        results = await ExerciseResultRepo.get_by_session(db_session, sess.id)
        assert len(results) == 1
        assert results[0].topic == "food"

    async def test_get_topic_average(self, db_session: AsyncSession, make_user):
        user = await make_user()
        for score in [6, 8, 10]:
            await ExerciseResultRepo.create(
                db_session, user_id=user.telegram_id,
                exercise_type="fill_blank", topic="verbs", score=score,
            )
        avg = await ExerciseResultRepo.get_topic_average(
            db_session, user.telegram_id, "verbs", last_n=3,
        )
        assert avg == pytest.approx(8.0)

    async def test_get_topic_average_no_data(self, db_session: AsyncSession, make_user):
        user = await make_user()
        avg = await ExerciseResultRepo.get_topic_average(
            db_session, user.telegram_id, "nonexistent",
        )
        assert avg is None

    async def test_delete_for_user(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="a", score=5,
        )
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="b", score=5,
        )
        deleted = await ExerciseResultRepo.delete_for_user(db_session, user.telegram_id)
        assert deleted == 2


class TestExerciseCheckConstraint:

    async def test_score_below_zero(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ExerciseResultRepo.create(
                db_session, user_id=user.telegram_id,
                exercise_type="fill_blank", topic="x", score=-1,
            )

    async def test_score_above_ten(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ExerciseResultRepo.create(
                db_session, user_id=user.telegram_id,
                exercise_type="fill_blank", topic="x", score=11,
            )


# ===========================================================================
# NotificationRepo
# ===========================================================================

class TestNotificationCRUD:

    async def test_create_and_get_recent(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await NotificationRepo.create(
            db_session,
            user_id=user.telegram_id,
            notification_type="daily_review",
            tier="template",
            trigger_source="schedule",
            message_text="Time to review!",
        )
        recent = await NotificationRepo.get_recent(db_session, user.telegram_id)
        assert len(recent) == 1
        assert recent[0].message_text == "Time to review!"
        assert recent[0].status == "sent"

    async def test_count_sent_today(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="msg1",
            status="sent",
        )
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="msg2",
            status="skipped_quiet",
        )

        count = await NotificationRepo.count_sent_today(db_session, user.telegram_id)
        assert count == 1  # only 'sent' status

    async def test_get_status_counts(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="a",
            status="sent",
        )
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="b",
            status="failed",
        )

        counts = await NotificationRepo.get_status_counts(db_session, days=1)
        assert counts.get("sent", 0) >= 1
        assert counts.get("failed", 0) >= 1

    async def test_get_failure_rate_recent(self, db_session: AsyncSession, make_user):
        user = await make_user()
        for status in ["sent", "sent", "sent", "failed", "failed"]:
            await NotificationRepo.create(
                db_session, user_id=user.telegram_id,
                notification_type="review", tier="template",
                trigger_source="schedule", message_text="x",
                status=status,
            )

        failed, total = await NotificationRepo.get_failure_rate_recent(
            db_session, hours=1,
        )
        assert failed >= 2
        assert total >= 5

    async def test_list_recent_all(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="admin view",
        )
        results = await NotificationRepo.list_recent_all(db_session)
        assert len(results) >= 1


class TestNotificationCheckConstraint:

    async def test_invalid_status(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await NotificationRepo.create(
                db_session, user_id=user.telegram_id,
                notification_type="review", tier="template",
                trigger_source="schedule", message_text="x",
                status="invalid_status",
            )


# ===========================================================================
# VocabularyReviewLogRepo
# ===========================================================================

class TestVocabularyReviewLogCRUD:

    async def test_create_and_get_for_vocab(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="test_log",
        )
        log = await VocabularyReviewLogRepo.create(
            db_session,
            user_id=user.telegram_id,
            vocabulary_id=vocab.id,
            rating=3,
        )
        assert log.id is not None

        logs = await VocabularyReviewLogRepo.get_for_vocab(db_session, vocab.id)
        assert len(logs) == 1
        assert logs[0].rating == 3

    async def test_ordering_most_recent_first(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="order_test",
        )
        log1 = await VocabularyReviewLogRepo.create(
            db_session, user_id=user.telegram_id,
            vocabulary_id=vocab.id, rating=2,
        )
        log2 = await VocabularyReviewLogRepo.create(
            db_session, user_id=user.telegram_id,
            vocabulary_id=vocab.id, rating=4,
        )

        logs = await VocabularyReviewLogRepo.get_for_vocab(db_session, vocab.id)
        assert logs[0].id == log2.id  # most recent first


class TestReviewLogCheckConstraint:

    async def test_rating_below_one(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="bad_rating",
        )
        with pytest.raises(IntegrityError):
            await VocabularyReviewLogRepo.create(
                db_session, user_id=user.telegram_id,
                vocabulary_id=vocab.id, rating=0,
            )

    async def test_rating_above_four(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="bad_rating2",
        )
        with pytest.raises(IntegrityError):
            await VocabularyReviewLogRepo.create(
                db_session, user_id=user.telegram_id,
                vocabulary_id=vocab.id, rating=5,
            )
