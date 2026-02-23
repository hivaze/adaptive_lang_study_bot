"""Integration tests for VocabularyRepo against real PostgreSQL."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import Vocabulary, VocabularyReviewLog
from adaptive_lang_study_bot.db.repositories import VocabularyRepo, VocabularyReviewLogRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------

class TestVocabularyCRUD:

    async def test_add_and_get(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="hola",
            translation="hello",
            topic="greetings",
        )
        assert vocab.id is not None

        fetched = await VocabularyRepo.get(db_session, vocab.id)
        assert fetched is not None
        assert fetched.word == "hola"
        assert fetched.translation == "hello"
        assert fetched.topic == "greetings"
        assert fetched.fsrs_state == 0  # new card
        assert fetched.review_count == 0

    async def test_unique_constraint_user_word(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="gato",
        )
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=user.telegram_id, word="gato",
            )

    async def test_unique_constraint_case_insensitive(self, db_session: AsyncSession, make_user):
        """The unique index is case-insensitive: 'Gato' and 'gato' conflict."""
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="Gato",
        )
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=user.telegram_id, word="gato",
            )

    async def test_same_word_different_users(self, db_session: AsyncSession, make_user):
        u1 = await make_user()
        u2 = await make_user()
        v1 = await VocabularyRepo.add(db_session, user_id=u1.telegram_id, word="gato")
        v2 = await VocabularyRepo.add(db_session, user_id=u2.telegram_id, word="gato")
        assert v1.id != v2.id

    async def test_fk_nonexistent_user(self, db_session: AsyncSession):
        with pytest.raises(IntegrityError):
            await VocabularyRepo.add(
                db_session, user_id=999_999_999, word="orphan",
            )


# ---------------------------------------------------------------------------
# Word lookup
# ---------------------------------------------------------------------------

class TestWordLookup:

    async def test_get_by_word_exact(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="Casa")

        assert await VocabularyRepo.get_by_word(db_session, user.telegram_id, "Casa") is not None
        assert await VocabularyRepo.get_by_word(db_session, user.telegram_id, "casa") is None

    async def test_get_by_word_ci(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="Casa")

        result = await VocabularyRepo.get_by_word_ci(db_session, user.telegram_id, "casa")
        assert result is not None
        assert result.word == "Casa"


# ---------------------------------------------------------------------------
# Due cards (FSRS)
# ---------------------------------------------------------------------------

class TestDueCards:

    async def test_get_due_returns_overdue(self, db_session: AsyncSession, make_user):
        user = await make_user()
        now = datetime.now(timezone.utc)

        # Overdue card
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="overdue",
            fsrs_due=now - timedelta(hours=1),
        )
        # Future card
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="future",
            fsrs_due=now + timedelta(days=7),
        )

        due = await VocabularyRepo.get_due(db_session, user.telegram_id)
        words = [v.word for v in due]
        assert "overdue" in words
        assert "future" not in words

    async def test_get_due_ordering(self, db_session: AsyncSession, make_user):
        user = await make_user()
        now = datetime.now(timezone.utc)

        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="recent",
            fsrs_due=now - timedelta(minutes=5),
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="old",
            fsrs_due=now - timedelta(days=3),
        )

        due = await VocabularyRepo.get_due(db_session, user.telegram_id)
        assert due[0].word == "old"  # most overdue first

    async def test_get_due_topic_filter(self, db_session: AsyncSession, make_user):
        user = await make_user()
        now = datetime.now(timezone.utc)

        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="comida",
            topic="food", fsrs_due=now - timedelta(hours=1),
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="rojo",
            topic="colors", fsrs_due=now - timedelta(hours=1),
        )

        due = await VocabularyRepo.get_due(db_session, user.telegram_id, topic="food")
        assert len(due) == 1
        assert due[0].word == "comida"

    async def test_count_due(self, db_session: AsyncSession, make_user):
        user = await make_user()
        now = datetime.now(timezone.utc)
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="w1",
            fsrs_due=now - timedelta(hours=1),
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="w2",
            fsrs_due=now + timedelta(days=1),
        )

        count = await VocabularyRepo.count_due(db_session, user.telegram_id)
        assert count == 1


# ---------------------------------------------------------------------------
# Search & topic
# ---------------------------------------------------------------------------

class TestSearchAndTopic:

    async def test_search_by_word(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id,
            word="mariposa", translation="butterfly",
        )
        results = await VocabularyRepo.search(db_session, user.telegram_id, "marip")
        assert len(results) == 1
        assert results[0].word == "mariposa"

    async def test_search_by_translation(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id,
            word="perro", translation="dog",
        )
        results = await VocabularyRepo.search(db_session, user.telegram_id, "dog")
        assert len(results) == 1

    async def test_get_by_topic(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="azul", topic="colors",
        )
        await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="pan", topic="food",
        )
        results = await VocabularyRepo.get_by_topic(
            db_session, user.telegram_id, "colors",
        )
        assert len(results) == 1
        assert results[0].word == "azul"

    async def test_count_for_user(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="uno")
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="dos")

        count = await VocabularyRepo.count_for_user(db_session, user.telegram_id)
        assert count == 2


# ---------------------------------------------------------------------------
# FSRS update
# ---------------------------------------------------------------------------

class TestUpdateFSRS:

    async def test_update_fsrs_fields(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="test",
        )
        now = datetime.now(timezone.utc)

        await VocabularyRepo.update_fsrs(
            db_session, vocab.id,
            fsrs_state=2,
            fsrs_stability=5.0,
            fsrs_difficulty=3.5,
            fsrs_due=now + timedelta(days=5),
            fsrs_last_review=now,
            fsrs_data={"step": 1},
            last_rating=3,
        )
        await db_session.refresh(vocab)

        assert vocab.fsrs_state == 2
        assert vocab.fsrs_stability == 5.0
        assert vocab.fsrs_difficulty == 3.5
        assert vocab.review_count == 1
        assert vocab.last_rating == 3


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteForUser:

    async def test_delete_returns_count(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="a")
        await VocabularyRepo.add(db_session, user_id=user.telegram_id, word="b")

        deleted = await VocabularyRepo.delete_for_user(db_session, user.telegram_id)
        assert deleted == 2

    async def test_delete_cascades_review_logs(self, db_session: AsyncSession, make_user):
        user = await make_user()
        vocab = await VocabularyRepo.add(
            db_session, user_id=user.telegram_id, word="logged",
        )
        await VocabularyReviewLogRepo.create(
            db_session,
            user_id=user.telegram_id,
            vocabulary_id=vocab.id,
            rating=3,
        )
        # Delete vocabulary — review log should cascade
        await VocabularyRepo.delete_for_user(db_session, user.telegram_id)
        logs = await VocabularyReviewLogRepo.get_for_vocab(db_session, vocab.id)
        assert len(logs) == 0


# ---------------------------------------------------------------------------
# Search ordering (Fix #17)
# ---------------------------------------------------------------------------

class TestSearchOrdering:

    async def test_search_results_are_deterministic(self, db_session: AsyncSession, make_user):
        """search() with LIMIT should return consistent ordering."""
        user = await make_user()
        words = [f"test_word_{i:03d}" for i in range(25)]
        for w in words:
            await VocabularyRepo.add(db_session, user_id=user.telegram_id, word=w)

        results1 = await VocabularyRepo.search(db_session, user.telegram_id, "test_word")
        results2 = await VocabularyRepo.search(db_session, user.telegram_id, "test_word")

        assert len(results1) == 20  # LIMIT 20
        # Same ordering both times
        assert [r.word for r in results1] == [r.word for r in results2]

    async def test_search_results_are_alphabetical(self, db_session: AsyncSession, make_user):
        """search() should return results in alphabetical order."""
        user = await make_user()
        for w in ["zebra", "apple", "mango"]:
            await VocabularyRepo.add(db_session, user_id=user.telegram_id, word=w)

        results = await VocabularyRepo.search(db_session, user.telegram_id, "")
        words = [r.word for r in results]
        assert words == sorted(words)

    async def test_get_by_topic_results_are_deterministic(self, db_session: AsyncSession, make_user):
        user = await make_user()
        for i in range(25):
            await VocabularyRepo.add(
                db_session,
                user_id=user.telegram_id,
                word=f"topic_word_{i:03d}",
                topic="food",
            )

        results1 = await VocabularyRepo.get_by_topic(db_session, user.telegram_id, "food")
        results2 = await VocabularyRepo.get_by_topic(db_session, user.telegram_id, "food")
        assert [r.word for r in results1] == [r.word for r in results2]
