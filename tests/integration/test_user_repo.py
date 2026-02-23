"""Integration tests for UserRepo against real PostgreSQL."""

from datetime import date, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import User, Vocabulary, Session as SessionModel
from adaptive_lang_study_bot.db.repositories import UserRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------

class TestUserCRUD:

    async def test_create_and_get(self, db_session: AsyncSession, make_user):
        user = await make_user(
            first_name="Alice",
            telegram_username="alice_tg",
            native_language="en",
            target_language="de",
        )
        fetched = await UserRepo.get(db_session, user.telegram_id)
        assert fetched is not None
        assert fetched.first_name == "Alice"
        assert fetched.telegram_username == "alice_tg"
        assert fetched.target_language == "de"
        assert fetched.level == "A1"
        assert fetched.tier == "free"
        assert fetched.is_admin is False
        assert fetched.is_active is True
        assert fetched.streak_days == 0
        assert fetched.interests == []
        assert fetched.recent_scores == []

    async def test_get_nonexistent_returns_none(self, db_session: AsyncSession):
        result = await UserRepo.get(db_session, 999_999_999)
        assert result is None

    async def test_create_duplicate_pk_raises(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await make_user(telegram_id=user.telegram_id)

    async def test_delete_returns_true(self, db_session: AsyncSession, make_user):
        user = await make_user()
        deleted = await UserRepo.delete(db_session, user.telegram_id)
        assert deleted is True
        assert await UserRepo.get(db_session, user.telegram_id) is None

    async def test_delete_nonexistent_returns_false(self, db_session: AsyncSession):
        deleted = await UserRepo.delete(db_session, 999_999_999)
        assert deleted is False


# ---------------------------------------------------------------------------
# update_fields
# ---------------------------------------------------------------------------

class TestUpdateFields:

    async def test_partial_update(self, db_session: AsyncSession, make_user):
        user = await make_user(first_name="Bob")
        old_updated = user.updated_at

        await UserRepo.update_fields(
            db_session, user.telegram_id, first_name="Robert", tier="premium",
        )
        await db_session.refresh(user)

        assert user.first_name == "Robert"
        assert user.tier == "premium"
        assert user.updated_at > old_updated


# ---------------------------------------------------------------------------
# append_score
# ---------------------------------------------------------------------------

class TestAppendScore:

    async def test_append_to_empty(self, db_session: AsyncSession, make_user):
        user = await make_user()
        scores = await UserRepo.append_score(db_session, user.telegram_id, 8)
        assert scores == [8]

    async def test_rolling_cap(self, db_session: AsyncSession, make_user):
        user = await make_user()
        for i in range(25):
            scores = await UserRepo.append_score(db_session, user.telegram_id, i)
        assert len(scores) == 20
        assert scores[0] == 5  # first 5 were dropped
        assert scores[-1] == 24

    async def test_missing_user_raises(self, db_session: AsyncSession):
        with pytest.raises(ValueError, match="not found"):
            await UserRepo.append_score(db_session, 999_999_999, 5)

    async def test_concurrent_append_no_lost_scores(self, db_session: AsyncSession, make_user):
        """Two sequential appends should both be reflected (atomic UPDATE)."""
        user = await make_user()
        await UserRepo.append_score(db_session, user.telegram_id, 7)
        scores = await UserRepo.append_score(db_session, user.telegram_id, 9)
        assert scores == [7, 9]


# ---------------------------------------------------------------------------
# update_streak
# ---------------------------------------------------------------------------

class TestUpdateStreak:

    async def test_first_call_sets_streak_to_1(self, db_session: AsyncSession, make_user):
        user = await make_user()
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1

    async def test_same_day_is_noop(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await UserRepo.update_streak(db_session, user.telegram_id)
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1

    async def test_gap_resets_streak(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await UserRepo.update_streak(db_session, user.telegram_id)
        # Simulate a gap of 3 days
        user.streak_updated_at = date.today() - timedelta(days=3)
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1


# ---------------------------------------------------------------------------
# Notification counter
# ---------------------------------------------------------------------------

class TestNotificationCounter:

    async def test_increment_and_reset(self, db_session: AsyncSession, make_user):
        user = await make_user()
        count = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        assert count == 1
        count = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        assert count == 2

        await UserRepo.reset_notification_counter(
            db_session, user.telegram_id, local_date=date.today(),
        )
        await db_session.refresh(user)
        assert user.notifications_sent_today == 0


# ---------------------------------------------------------------------------
# Listing & filtering
# ---------------------------------------------------------------------------

class TestListingAndFiltering:

    async def test_get_active_users_for_proactive(self, db_session: AsyncSession, make_user):
        active = await make_user(first_name="Active")
        inactive = await make_user(first_name="Inactive", is_active=False)

        users = await UserRepo.get_active_users_for_proactive(db_session)
        ids = [u.telegram_id for u in users]
        assert active.telegram_id in ids
        assert inactive.telegram_id not in ids

    async def test_get_active_users_pagination(self, db_session: AsyncSession, make_user):
        """Pagination (limit/offset) returns stable, ordered pages."""
        for i in range(5):
            await make_user(first_name=f"Page{i}")

        page1 = await UserRepo.get_active_users_for_proactive(db_session, limit=2, offset=0)
        page2 = await UserRepo.get_active_users_for_proactive(db_session, limit=2, offset=2)
        page3 = await UserRepo.get_active_users_for_proactive(db_session, limit=2, offset=4)

        page1_ids = [u.telegram_id for u in page1]
        page2_ids = [u.telegram_id for u in page2]

        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) >= 1  # at least 1 (5th user), could be more from other tests
        # No overlap between pages
        assert set(page1_ids).isdisjoint(set(page2_ids))
        # Results are ordered by telegram_id (PK)
        assert page1_ids == sorted(page1_ids)
        assert page2_ids == sorted(page2_ids)

    async def test_list_all_active_only(self, db_session: AsyncSession, make_user):
        await make_user(is_active=True)
        await make_user(is_active=False)

        active_list = await UserRepo.list_all(db_session, active_only=True)
        all_list = await UserRepo.list_all(db_session, active_only=False)
        assert len(all_list) >= len(active_list)

    async def test_count(self, db_session: AsyncSession, make_user):
        await make_user(is_active=True)
        await make_user(is_active=False)

        active_count = await UserRepo.count(db_session, active_only=True)
        all_count = await UserRepo.count(db_session, active_only=False)
        assert all_count >= active_count
        assert active_count >= 1

    async def test_get_tier_counts(self, db_session: AsyncSession, make_user):
        await make_user(tier="free")
        await make_user(tier="premium")

        counts = await UserRepo.get_tier_counts(db_session)
        assert counts.get("free", 0) >= 1
        assert counts.get("premium", 0) >= 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:

    async def test_search_by_telegram_id(self, db_session: AsyncSession, make_user):
        user = await make_user()
        results = await UserRepo.search(db_session, str(user.telegram_id))
        assert len(results) == 1
        assert results[0].telegram_id == user.telegram_id

    async def test_search_by_name(self, db_session: AsyncSession, make_user):
        await make_user(first_name="UniqueSearchName")
        results = await UserRepo.search(db_session, "UniqueSearch")
        assert len(results) >= 1
        assert any(u.first_name == "UniqueSearchName" for u in results)

    async def test_search_by_username(self, db_session: AsyncSession, make_user):
        await make_user(telegram_username="unique_handle_xyz")
        results = await UserRepo.search(db_session, "unique_handle")
        assert len(results) >= 1

    async def test_search_like_escape(self, db_session: AsyncSession, make_user):
        await make_user(first_name="100%_done")
        # The % should be escaped, not treated as wildcard
        results = await UserRepo.search(db_session, "100%")
        assert any(u.first_name == "100%_done" for u in results)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class TestAdmin:

    async def test_get_admins(self, db_session: AsyncSession, make_user):
        admin = await make_user(is_admin=True, first_name="Admin")
        regular = await make_user(is_admin=False, first_name="Regular")

        admins = await UserRepo.get_admins(db_session)
        admin_ids = [u.telegram_id for u in admins]
        assert admin.telegram_id in admin_ids
        assert regular.telegram_id not in admin_ids

    async def test_get_all_admin_ids(self, db_session: AsyncSession, make_user):
        admin = await make_user(is_admin=True)
        ids = await UserRepo.get_all_admin_ids(db_session)
        assert admin.telegram_id in ids


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------

class TestCheckConstraints:

    async def test_invalid_level(self, db_session: AsyncSession, make_user):
        with pytest.raises(IntegrityError):
            await make_user(level="X9")

    async def test_invalid_tier(self, db_session: AsyncSession, make_user):
        with pytest.raises(IntegrityError):
            await make_user(tier="vip")

    async def test_invalid_difficulty(self, db_session: AsyncSession, make_user):
        with pytest.raises(IntegrityError):
            await make_user(preferred_difficulty="extreme")

    async def test_invalid_style(self, db_session: AsyncSession, make_user):
        with pytest.raises(IntegrityError):
            await make_user(session_style="turbo")


# ---------------------------------------------------------------------------
# Milestones atomic operations
# ---------------------------------------------------------------------------

class TestMilestonesAtomic:

    async def test_clear_pending_celebrations(self, db_session: AsyncSession, make_user):
        """clear_pending_celebrations should empty the list while preserving other keys."""
        user = await make_user(milestones={
            "pending_celebrations": ["Great streak!"],
            "vocabulary_count": 42,
            "days_streak": 7,
        })
        await UserRepo.clear_pending_celebrations(db_session, user.telegram_id)
        await db_session.refresh(user)

        assert user.milestones["pending_celebrations"] == []
        assert user.milestones["vocabulary_count"] == 42
        assert user.milestones["days_streak"] == 7

    async def test_clear_pending_on_empty_milestones(self, db_session: AsyncSession, make_user):
        """clear_pending_celebrations should work on a user with empty milestones."""
        user = await make_user(milestones={})
        await UserRepo.clear_pending_celebrations(db_session, user.telegram_id)
        await db_session.refresh(user)

        assert user.milestones["pending_celebrations"] == []

    async def test_update_milestones_merges(self, db_session: AsyncSession, make_user):
        """update_milestones should merge new keys while preserving existing ones."""
        user = await make_user(milestones={
            "pending_celebrations": ["Well done!"],
            "vocabulary_count": 10,
        })
        await UserRepo.update_milestones(db_session, user.telegram_id, {
            "vocabulary_count": 20,
            "days_streak": 5,
        })
        await db_session.refresh(user)

        assert user.milestones["vocabulary_count"] == 20
        assert user.milestones["days_streak"] == 5
        # Existing key not in the update should be preserved
        assert user.milestones["pending_celebrations"] == ["Well done!"]
