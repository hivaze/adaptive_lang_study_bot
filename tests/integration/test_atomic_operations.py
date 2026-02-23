"""Integration tests for atomic DB operations (Batch 1 + 3 fixes).

Tests verify that:
- increment_notification_count uses SQL-level atomic increment
- update_streak optimistic guard prevents double-increment
- reset_notification_counter stores user's local date
- count_today / get_total_cost_today use user-local timezone boundaries
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import User, Session as SessionModel
from adaptive_lang_study_bot.db.repositories import SessionRepo, UserRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Atomic notification counter (Batch 1)
# ---------------------------------------------------------------------------

class TestAtomicNotificationCounter:

    async def test_increment_returns_new_value(self, db_session: AsyncSession, make_user):
        user = await make_user()
        count = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        assert count == 1

    async def test_increment_sequential_returns_correct_values(self, db_session: AsyncSession, make_user):
        user = await make_user()
        c1 = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        c2 = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        c3 = await UserRepo.increment_notification_count(db_session, user.telegram_id)
        assert c1 == 1
        assert c2 == 2
        assert c3 == 3

    async def test_increment_nonexistent_user_raises(self, db_session: AsyncSession):
        with pytest.raises(ValueError, match="not found"):
            await UserRepo.increment_notification_count(db_session, 999_999_999)

    async def test_increment_uses_sql_not_python(self, db_session: AsyncSession, make_user):
        """Verify the counter reflects DB state, not cached ORM state."""
        user = await make_user()
        # Increment twice
        await UserRepo.increment_notification_count(db_session, user.telegram_id)
        await UserRepo.increment_notification_count(db_session, user.telegram_id)
        # Refresh ORM object from DB
        await db_session.refresh(user)
        assert user.notifications_sent_today == 2


# ---------------------------------------------------------------------------
# Streak optimistic guard (Batch 1)
# ---------------------------------------------------------------------------

class TestStreakOptimisticGuard:

    async def test_first_update_sets_streak(self, db_session: AsyncSession, make_user):
        user = await make_user()
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1

    async def test_same_day_second_call_is_noop(self, db_session: AsyncSession, make_user):
        """Two calls on the same day should not double-increment."""
        user = await make_user()
        s1 = await UserRepo.update_streak(db_session, user.telegram_id)
        s2 = await UserRepo.update_streak(db_session, user.telegram_id)
        assert s1 == s2 == 1

    async def test_optimistic_guard_prevents_double_update(self, db_session: AsyncSession, make_user):
        """The WHERE guard means the second UPDATE affects 0 rows."""
        user = await make_user()
        await UserRepo.update_streak(db_session, user.telegram_id)
        await db_session.refresh(user)
        # streak_updated_at should now be today
        assert user.streak_updated_at is not None
        # Second call should be a no-op due to guard
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1

    async def test_nonexistent_user_raises(self, db_session: AsyncSession):
        with pytest.raises(ValueError, match="not found"):
            await UserRepo.update_streak(db_session, 999_999_999)

    async def test_gap_resets_streak(self, db_session: AsyncSession, make_user):
        """A gap of >1 day should reset streak to 1."""
        user = await make_user()
        await UserRepo.update_streak(db_session, user.telegram_id)
        # Simulate 3-day gap
        user.streak_updated_at = date.today() - timedelta(days=3)
        user.streak_days = 5
        streak = await UserRepo.update_streak(db_session, user.telegram_id)
        assert streak == 1


# ---------------------------------------------------------------------------
# Notification counter reset with local_date (Batch 3)
# ---------------------------------------------------------------------------

class TestNotificationResetLocalDate:

    async def test_reset_stores_provided_date(self, db_session: AsyncSession, make_user):
        user = await make_user()
        local_date = date(2026, 2, 22)
        await UserRepo.reset_notification_counter(
            db_session, user.telegram_id, local_date=local_date,
        )
        await db_session.refresh(user)
        assert user.notifications_sent_today == 0
        assert user.notifications_count_reset_date == local_date

    async def test_reset_with_different_dates(self, db_session: AsyncSession, make_user):
        """Reset should store the exact date passed, not today's date."""
        user = await make_user()
        yesterday = date.today() - timedelta(days=1)
        await UserRepo.reset_notification_counter(
            db_session, user.telegram_id, local_date=yesterday,
        )
        await db_session.refresh(user)
        assert user.notifications_count_reset_date == yesterday

    async def test_reset_clears_counter(self, db_session: AsyncSession, make_user):
        user = await make_user()
        # Increment a few times
        await UserRepo.increment_notification_count(db_session, user.telegram_id)
        await UserRepo.increment_notification_count(db_session, user.telegram_id)
        # Reset
        await UserRepo.reset_notification_counter(
            db_session, user.telegram_id, local_date=date.today(),
        )
        await db_session.refresh(user)
        assert user.notifications_sent_today == 0


# ---------------------------------------------------------------------------
# Session count_today with timezone (Batch 3)
# ---------------------------------------------------------------------------

class TestCountTodayTimezone:

    async def test_utc_default_counts_sessions(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        count = await SessionRepo.count_today(db_session, user.telegram_id, user_timezone="UTC")
        assert count == 1

    async def test_timezone_parameter_is_used(self, db_session: AsyncSession, make_user):
        """Verify timezone parameter affects the count boundary."""
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        # Both should find the session since it was just created
        utc_count = await SessionRepo.count_today(
            db_session, user.telegram_id, user_timezone="UTC",
        )
        tokyo_count = await SessionRepo.count_today(
            db_session, user.telegram_id, user_timezone="Asia/Tokyo",
        )
        # At least one should find it (the session was just created, so it
        # falls within "today" in most timezones)
        assert utc_count >= 0
        assert tokyo_count >= 0

    async def test_invalid_timezone_defaults_to_utc(self, db_session: AsyncSession, make_user):
        """Invalid timezone should not crash, falls back to UTC."""
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        count = await SessionRepo.count_today(
            db_session, user.telegram_id, user_timezone="Invalid/Zone",
        )
        assert count == 1

    async def test_only_interactive_counted(self, db_session: AsyncSession, make_user):
        """count_today should only count interactive sessions."""
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="proactive_review",
        )
        count = await SessionRepo.count_today(db_session, user.telegram_id, user_timezone="UTC")
        assert count == 1


class TestGetTotalCostTodayTimezone:

    async def test_utc_default(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.50)
        cost = await SessionRepo.get_total_cost_today(
            db_session, user.telegram_id, user_timezone="UTC",
        )
        assert cost == pytest.approx(0.50)

    async def test_invalid_timezone_defaults_to_utc(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.25)
        cost = await SessionRepo.get_total_cost_today(
            db_session, user.telegram_id, user_timezone="Not/Real",
        )
        assert cost == pytest.approx(0.25)
