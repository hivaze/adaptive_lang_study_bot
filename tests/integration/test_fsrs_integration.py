"""Integration tests for FSRS engine with real PostgreSQL round-trip.

Tests card creation, review scheduling, FSRS state persistence via
VocabularyRepo, and due-card queries after reviews.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import VocabularyRepo
from adaptive_lang_study_bot.fsrs_engine.scheduler import (
    create_new_card,
    get_card_state_name,
    review_card,
)

pytestmark = pytest.mark.integration


class TestFSRSCardCreation:

    async def test_new_card_stored_in_db(self, db_session: AsyncSession, make_user):
        """New FSRS card data should persist to DB via VocabularyRepo."""
        user = await make_user()
        card_data = create_new_card()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="hola",
            fsrs_state=card_data["state"],
            fsrs_stability=card_data["stability"],
            fsrs_difficulty=card_data["difficulty"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        fetched = await VocabularyRepo.get(db_session, vocab.id)
        assert fetched.fsrs_state == card_data["state"]
        assert fetched.fsrs_data is not None

    async def test_new_card_is_immediately_due(self, db_session: AsyncSession, make_user):
        """A newly created card should be due immediately (due <= now)."""
        user = await make_user()
        card_data = create_new_card()

        await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="ahora",
            fsrs_state=card_data["state"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        due = await VocabularyRepo.get_due(db_session, user.telegram_id)
        words = [v.word for v in due]
        assert "ahora" in words


class TestFSRSReviewRoundTrip:

    async def test_review_good_updates_db(self, db_session: AsyncSession, make_user):
        """Rating 'Good' should update FSRS state and push due date forward."""
        user = await make_user()
        card_data = create_new_card()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="bueno",
            fsrs_state=card_data["state"],
            fsrs_stability=card_data["stability"],
            fsrs_difficulty=card_data["difficulty"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        # Review with rating=3 (Good)
        result = review_card(vocab, rating=3)

        await VocabularyRepo.update_fsrs(
            db_session, vocab.id,
            fsrs_state=result["state"],
            fsrs_stability=result["stability"],
            fsrs_difficulty=result["difficulty"],
            fsrs_due=result["due"],
            fsrs_last_review=result["last_review"],
            fsrs_data=result["card_data"],
            last_rating=3,
        )
        await db_session.refresh(vocab)

        assert vocab.fsrs_state != 0  # No longer "New"
        assert vocab.fsrs_last_review is not None
        assert vocab.review_count == 1
        assert vocab.last_rating == 3

    async def test_review_again_keeps_due_soon(self, db_session: AsyncSession, make_user):
        """Rating 'Again' should keep the card due very soon."""
        user = await make_user()
        card_data = create_new_card()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="difícil",
            fsrs_state=card_data["state"],
            fsrs_stability=card_data["stability"],
            fsrs_difficulty=card_data["difficulty"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        result = review_card(vocab, rating=1)  # Again

        await VocabularyRepo.update_fsrs(
            db_session, vocab.id,
            fsrs_state=result["state"],
            fsrs_stability=result["stability"],
            fsrs_difficulty=result["difficulty"],
            fsrs_due=result["due"],
            fsrs_last_review=result["last_review"],
            fsrs_data=result["card_data"],
            last_rating=1,
        )
        await db_session.refresh(vocab)

        # "Again" should schedule within minutes, not days
        assert result["scheduled_days"] < 1

    async def test_review_easy_schedules_far_out(self, db_session: AsyncSession, make_user):
        """Rating 'Easy' should schedule the card further out than 'Hard'."""
        user = await make_user()
        card_data = create_new_card()

        vocab_easy = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="fácil",
            fsrs_state=card_data["state"],
            fsrs_stability=card_data["stability"],
            fsrs_difficulty=card_data["difficulty"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        card_data2 = create_new_card()
        vocab_hard = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="duro",
            fsrs_state=card_data2["state"],
            fsrs_stability=card_data2["stability"],
            fsrs_difficulty=card_data2["difficulty"],
            fsrs_due=card_data2["due"],
            fsrs_data=card_data2["card_data"],
        )

        result_easy = review_card(vocab_easy, rating=4)  # Easy
        result_hard = review_card(vocab_hard, rating=2)  # Hard

        # Easy should be scheduled further out than Hard
        assert result_easy["scheduled_days"] >= result_hard["scheduled_days"]

    async def test_multiple_reviews_progression(self, db_session: AsyncSession, make_user):
        """Multiple 'Good' reviews should progressively increase intervals."""
        user = await make_user()
        card_data = create_new_card()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="progreso",
            fsrs_state=card_data["state"],
            fsrs_stability=card_data["stability"],
            fsrs_difficulty=card_data["difficulty"],
            fsrs_due=card_data["due"],
            fsrs_data=card_data["card_data"],
        )

        intervals = []
        for _ in range(3):
            result = review_card(vocab, rating=3)  # Good
            intervals.append(result["scheduled_days"])

            await VocabularyRepo.update_fsrs(
                db_session, vocab.id,
                fsrs_state=result["state"],
                fsrs_stability=result["stability"],
                fsrs_difficulty=result["difficulty"],
                fsrs_due=result["due"],
                fsrs_last_review=result["last_review"],
                fsrs_data=result["card_data"],
                last_rating=3,
            )
            await db_session.refresh(vocab)

        # Intervals should generally increase (spaced repetition)
        assert intervals[-1] >= intervals[0]

    async def test_reviewed_card_no_longer_due(self, db_session: AsyncSession, make_user):
        """After a 'Good' review, the card should no longer be due."""
        user = await make_user()
        now = datetime.now(timezone.utc)
        card_data = create_new_card()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="revisado",
            fsrs_state=card_data["state"],
            fsrs_due=now - timedelta(hours=1),  # overdue
            fsrs_data=card_data["card_data"],
        )

        # Verify it's due
        due_before = await VocabularyRepo.get_due(db_session, user.telegram_id)
        assert any(v.word == "revisado" for v in due_before)

        # Review it
        result = review_card(vocab, rating=3)
        await VocabularyRepo.update_fsrs(
            db_session, vocab.id,
            fsrs_state=result["state"],
            fsrs_stability=result["stability"],
            fsrs_difficulty=result["difficulty"],
            fsrs_due=result["due"],
            fsrs_last_review=result["last_review"],
            fsrs_data=result["card_data"],
            last_rating=3,
        )

        # Should no longer be due
        due_after = await VocabularyRepo.get_due(db_session, user.telegram_id)
        assert not any(v.word == "revisado" for v in due_after)


class TestFSRSCorruptedData:

    async def test_corrupted_fsrs_data_resets_card(self, db_session: AsyncSession, make_user):
        """Corrupted fsrs_data should be handled gracefully by resetting the card."""
        user = await make_user()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="corrupted",
            fsrs_data={"invalid": "data"},
        )

        # review_card should handle corrupted data without crashing
        result = review_card(vocab, rating=3)
        assert result["state"] is not None
        assert result["due"] is not None

    async def test_empty_fsrs_data_handled(self, db_session: AsyncSession, make_user):
        """Empty fsrs_data should create a new card for review."""
        user = await make_user()

        vocab = await VocabularyRepo.add(
            db_session,
            user_id=user.telegram_id,
            word="empty",
            fsrs_data={},
        )

        result = review_card(vocab, rating=4)
        assert result["state"] is not None
        assert result["scheduled_days"] >= 0


class TestCardStateNames:

    def test_all_states_have_names(self):
        for state in [0, 1, 2, 3]:
            name = get_card_state_name(state)
            assert name != "Unknown"

    def test_unknown_state(self):
        assert get_card_state_name(99) == "Unknown"
