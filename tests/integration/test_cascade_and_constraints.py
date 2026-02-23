"""Integration tests for FK cascades and cross-table constraint enforcement."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import (
    ExerciseResult,
    Notification,
    Schedule,
    Session as SessionModel,
    User,
    Vocabulary,
    VocabularyReviewLog,
)
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    NotificationRepo,
    ScheduleRepo,
    SessionRepo,
    UserRepo,
    VocabularyRepo,
    VocabularyReviewLogRepo,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# User CASCADE delete
# ---------------------------------------------------------------------------

class TestUserCascadeDelete:

    async def test_delete_user_cascades_vocabulary(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="cascade_test",
        )
        vocab_id = vocab.id
        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()
        db_session.expire_all()  # clear identity map so get() hits DB

        assert await VocabularyRepo.get(db_session, vocab_id) is None

    async def test_delete_user_cascades_sessions(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        sess_id = sess.id
        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()
        db_session.expire_all()

        assert await SessionRepo.get(db_session, sess_id) is None

    async def test_delete_user_cascades_schedules(self, db_session: AsyncSession, make_user):
        user = await make_user()
        from datetime import datetime, timezone
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=datetime.now(timezone.utc),
            description="cascade test",
        )
        sched_id = sched.id
        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()
        db_session.expire_all()

        assert await ScheduleRepo.get(db_session, sched_id) is None

    async def test_delete_user_cascades_exercise_results(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="test", score=5,
        )
        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()

        results = await ExerciseResultRepo.get_recent(db_session, user.telegram_id)
        assert len(results) == 0

    async def test_delete_user_cascades_notifications(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="template",
            trigger_source="schedule", message_text="cascade",
        )
        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()

        recent = await NotificationRepo.get_recent(db_session, user.telegram_id)
        assert len(recent) == 0


# ---------------------------------------------------------------------------
# Vocabulary CASCADE → review logs
# ---------------------------------------------------------------------------

class TestVocabularyCascade:

    async def test_delete_vocabulary_cascades_review_logs(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="with_logs",
        )
        await VocabularyReviewLogRepo.create(
            db_session, user_id=user.telegram_id,
            vocabulary_id=vocab.id, rating=3,
        )
        await VocabularyReviewLogRepo.create(
            db_session, user_id=user.telegram_id,
            vocabulary_id=vocab.id, rating=4,
        )

        # Delete vocabulary
        await VocabularyRepo.delete_for_user(db_session, user.telegram_id)
        await db_session.flush()

        logs = await VocabularyReviewLogRepo.get_for_vocab(db_session, vocab.id)
        assert len(logs) == 0


# ---------------------------------------------------------------------------
# Session delete → SET NULL on exercise_results and notifications
# ---------------------------------------------------------------------------

class TestSessionSetNull:

    async def test_delete_session_nullifies_exercise_result_fk(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        er = await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            session_id=sess.id,
            exercise_type="fill_blank", topic="test", score=7,
        )

        # Delete the session via raw delete (CASCADE should SET NULL)
        from sqlalchemy import delete
        await db_session.execute(
            delete(SessionModel).where(SessionModel.id == sess.id),
        )
        await db_session.flush()
        await db_session.refresh(er)

        assert er.session_id is None  # SET NULL

    async def test_delete_session_nullifies_notification_fk(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        notif = await NotificationRepo.create(
            db_session, user_id=user.telegram_id,
            notification_type="review", tier="llm",
            trigger_source="schedule", message_text="llm msg",
            session_id=sess.id,
        )

        from sqlalchemy import delete
        await db_session.execute(
            delete(SessionModel).where(SessionModel.id == sess.id),
        )
        await db_session.flush()
        await db_session.refresh(notif)

        assert notif.session_id is None  # SET NULL


# ---------------------------------------------------------------------------
# UNIQUE constraints
# ---------------------------------------------------------------------------

class TestUniqueConstraints:

    async def test_vocabulary_user_word_unique(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="unique_word",
        )
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=user.telegram_id, word="unique_word",
            )

    async def test_vocabulary_user_word_case_insensitive(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="Unique_Word",
        )
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=user.telegram_id, word="unique_word",
            )


# ---------------------------------------------------------------------------
# FK constraints
# ---------------------------------------------------------------------------

class TestForeignKeyConstraints:

    async def test_vocabulary_requires_valid_user(self, db_session: AsyncSession):
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=999_999_999, word="orphan",
            )

    async def test_session_requires_valid_user(self, db_session: AsyncSession):
        with pytest.raises(IntegrityError):
            await SessionRepo.create(
                db_session, user_id=999_999_999, session_type="interactive",
            )

    async def test_review_log_requires_valid_vocabulary(
        self, db_session: AsyncSession, make_user,
    ):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await VocabularyReviewLogRepo.create(
                db_session, user_id=user.telegram_id,
                vocabulary_id=999_999_999, rating=3,
            )
