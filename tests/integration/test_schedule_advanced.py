"""Integration tests for advanced schedule operations.

Covers ScheduleRepo.get_due with selectinload (eager user loading),
multi-schedule scenarios, pause_until behavior, and edge cases.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import ScheduleRepo

pytestmark = pytest.mark.integration

_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# get_due with selectinload — eager user loading
# ---------------------------------------------------------------------------

class TestGetDueEagerLoading:

    async def test_get_due_loads_user_relationship(self, db_session: AsyncSession, make_user):
        """get_due should eagerly load the user relationship (selectinload).

        Without selectinload, accessing schedule.user in async context would
        raise MissingGreenlet. This test verifies that user data is loaded.
        """
        user = await make_user(first_name="EagerTest", timezone="Asia/Tokyo")
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="due",
        )

        due = await ScheduleRepo.get_due(db_session)
        assert len(due) >= 1

        # Access user relationship — should NOT raise MissingGreenlet
        schedule = next(s for s in due if s.user_id == user.telegram_id)
        assert schedule.user is not None
        assert schedule.user.first_name == "EagerTest"
        assert schedule.user.timezone == "Asia/Tokyo"

    async def test_get_due_multiple_users(self, db_session: AsyncSession, make_user):
        """get_due with multiple users should load all user relationships."""
        u1 = await make_user(first_name="User1")
        u2 = await make_user(first_name="User2")

        await ScheduleRepo.create(
            db_session,
            user_id=u1.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="u1 due",
        )
        await ScheduleRepo.create(
            db_session,
            user_id=u2.telegram_id,
            schedule_type="quiz",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=3),
            description="u2 due",
        )

        due = await ScheduleRepo.get_due(db_session)
        due_user_ids = {s.user_id for s in due}
        assert u1.telegram_id in due_user_ids
        assert u2.telegram_id in due_user_ids

        # All user relationships should be loaded
        for sched in due:
            if sched.user_id in {u1.telegram_id, u2.telegram_id}:
                assert sched.user is not None


# ---------------------------------------------------------------------------
# Multi-schedule scenarios
# ---------------------------------------------------------------------------

class TestMultiSchedule:

    async def test_user_max_schedules(self, db_session: AsyncSession, make_user):
        """Should be able to create up to 10 schedules per user."""
        user = await make_user()
        types = ["daily_review", "quiz", "progress_report", "practice_reminder", "custom"]

        for i in range(10):
            stype = types[i % len(types)]
            await ScheduleRepo.create(
                db_session,
                user_id=user.telegram_id,
                schedule_type=stype,
                rrule="FREQ=DAILY",
                next_trigger_at=_NOW + timedelta(hours=i + 1),
                description=f"schedule_{i}",
            )

        all_scheds = await ScheduleRepo.get_for_user(
            db_session, user.telegram_id, active_only=False,
        )
        assert len(all_scheds) == 10

    async def test_count_for_user_active_vs_all(self, db_session: AsyncSession, make_user):
        """count_for_user should correctly filter by active_only."""
        user = await make_user()

        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            status="active",
            description="active",
        )
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="quiz",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            status="paused",
            description="paused",
        )

        active_count = await ScheduleRepo.count_for_user(
            db_session, user.telegram_id, active_only=True,
        )
        all_count = await ScheduleRepo.count_for_user(
            db_session, user.telegram_id, active_only=False,
        )
        assert active_count == 1
        assert all_count == 2


# ---------------------------------------------------------------------------
# Update after trigger — cumulative behavior
# ---------------------------------------------------------------------------

class TestTriggerAccumulation:

    async def test_multiple_triggers_accumulate(self, db_session: AsyncSession, make_user):
        """trigger_count should accumulate over multiple successful triggers."""
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="multi-trigger",
        )

        for i in range(3):
            await ScheduleRepo.update_after_trigger(
                db_session, sched.id,
                next_trigger_at=_NOW + timedelta(hours=(i + 1) * 24),
                success=True,
            )

        await db_session.refresh(sched)
        assert sched.trigger_count == 3
        assert sched.consecutive_failures == 0

    async def test_failure_then_success_resets_failures(
        self, db_session: AsyncSession, make_user,
    ):
        """consecutive_failures should reset to 0 after a successful trigger."""
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="recovery",
        )

        # 3 failures
        for i in range(3):
            await ScheduleRepo.update_after_trigger(
                db_session, sched.id,
                next_trigger_at=_NOW + timedelta(hours=1),
                success=False,
            )
        await db_session.refresh(sched)
        assert sched.consecutive_failures == 3

        # 1 success
        await ScheduleRepo.update_after_trigger(
            db_session, sched.id,
            next_trigger_at=_NOW + timedelta(hours=24),
            success=True,
        )
        await db_session.refresh(sched)
        assert sched.consecutive_failures == 0
        assert sched.trigger_count == 4  # 3 failures + 1 success


# ---------------------------------------------------------------------------
# Delete for user by type
# ---------------------------------------------------------------------------

class TestDeleteForUserByType:

    async def test_delete_only_specified_type(self, db_session: AsyncSession, make_user):
        """delete_for_user should only delete schedules of the specified type."""
        user = await make_user()

        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="keep",
        )
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="quiz",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="delete1",
        )
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="quiz",
            rrule="FREQ=WEEKLY",
            next_trigger_at=_NOW,
            description="delete2",
        )

        deleted = await ScheduleRepo.delete_for_user(
            db_session, user.telegram_id, "quiz",
        )
        assert deleted == 2

        remaining = await ScheduleRepo.get_for_user(
            db_session, user.telegram_id, active_only=False,
        )
        assert len(remaining) == 1
        assert remaining[0].schedule_type == "daily_review"

    async def test_delete_nonexistent_type_returns_zero(
        self, db_session: AsyncSession, make_user,
    ):
        """Deleting a type that doesn't exist should return 0."""
        user = await make_user()
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="keep",
        )

        deleted = await ScheduleRepo.delete_for_user(
            db_session, user.telegram_id, "quiz",
        )
        assert deleted == 0
