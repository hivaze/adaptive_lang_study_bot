"""Integration tests for ScheduleRepo against real PostgreSQL."""

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.repositories import ScheduleRepo

pytestmark = pytest.mark.integration

_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------

class TestScheduleCRUD:

    async def test_create_and_get(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY;BYHOUR=9",
            next_trigger_at=_NOW + timedelta(hours=1),
            description="Daily morning review",
        )
        assert sched.id is not None

        fetched = await ScheduleRepo.get(db_session, sched.id)
        assert fetched is not None
        assert fetched.schedule_type == "daily_review"
        assert fetched.status == "active"
        assert fetched.rrule == "FREQ=DAILY;BYHOUR=9"
        assert fetched.trigger_count == 0
        assert fetched.consecutive_failures == 0
        assert fetched.notification_tier == "template"

    async def test_get_nonexistent(self, db_session: AsyncSession):
        import uuid
        result = await ScheduleRepo.get(db_session, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# get_due
# ---------------------------------------------------------------------------

class TestGetDue:

    async def test_returns_overdue_active_only(self, db_session: AsyncSession, make_user):
        user = await make_user()

        # Due: active + past trigger
        due_sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="overdue",
        )
        # Not due: active + future trigger
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="quiz",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW + timedelta(hours=2),
            description="future",
        )
        # Not due: paused + past trigger
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="practice_reminder",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            status="paused",
            description="paused",
        )

        due = await ScheduleRepo.get_due(db_session)
        due_ids = [s.id for s in due]
        assert due_sched.id in due_ids


# ---------------------------------------------------------------------------
# get_for_user / count_for_user
# ---------------------------------------------------------------------------

class TestForUser:

    async def test_get_for_user_active_only(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
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

        active = await ScheduleRepo.get_for_user(db_session, user.telegram_id, active_only=True)
        all_scheds = await ScheduleRepo.get_for_user(db_session, user.telegram_id, active_only=False)
        assert len(active) == 1
        assert len(all_scheds) == 2

    async def test_count_for_user(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="one",
        )
        count = await ScheduleRepo.count_for_user(db_session, user.telegram_id)
        assert count == 1


# ---------------------------------------------------------------------------
# update_after_trigger
# ---------------------------------------------------------------------------

class TestUpdateAfterTrigger:

    async def test_success_resets_failures(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW - timedelta(minutes=5),
            description="test",
        )
        next_trigger = _NOW + timedelta(hours=24)
        await ScheduleRepo.update_after_trigger(
            db_session, sched.id, next_trigger_at=next_trigger, success=True,
        )
        await db_session.refresh(sched)

        assert sched.trigger_count == 1
        assert sched.consecutive_failures == 0
        assert sched.last_triggered_at is not None
        assert sched.next_trigger_at == next_trigger

    async def test_failure_increments_consecutive(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="test",
        )
        next_trigger = _NOW + timedelta(hours=24)
        await ScheduleRepo.update_after_trigger(
            db_session, sched.id, next_trigger_at=next_trigger, success=False,
        )
        await db_session.refresh(sched)

        assert sched.trigger_count == 1
        assert sched.consecutive_failures == 1


# ---------------------------------------------------------------------------
# update_fields / delete
# ---------------------------------------------------------------------------

class TestUpdateAndDelete:

    async def test_update_fields(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="original",
        )
        await ScheduleRepo.update_fields(
            db_session, sched.id, status="paused", description="updated",
        )
        await db_session.refresh(sched)
        assert sched.status == "paused"
        assert sched.description == "updated"

    async def test_delete(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY",
            next_trigger_at=_NOW,
            description="del me",
        )
        await ScheduleRepo.delete(db_session, sched.id)
        assert await ScheduleRepo.get(db_session, sched.id) is None

    async def test_delete_for_user_by_type(self, db_session: AsyncSession, make_user):
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
            description="delete",
        )
        deleted = await ScheduleRepo.delete_for_user(
            db_session, user.telegram_id, "quiz",
        )
        assert deleted == 1
        remaining = await ScheduleRepo.get_for_user(
            db_session, user.telegram_id, active_only=False,
        )
        assert len(remaining) == 1
        assert remaining[0].schedule_type == "daily_review"


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------

class TestScheduleCheckConstraints:

    async def test_invalid_schedule_type(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ScheduleRepo.create(
                db_session,
                user_id=user.telegram_id,
                schedule_type="invalid_type",
                rrule="FREQ=DAILY",
                next_trigger_at=_NOW,
                description="bad",
            )

    async def test_invalid_status(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ScheduleRepo.create(
                db_session,
                user_id=user.telegram_id,
                schedule_type="daily_review",
                rrule="FREQ=DAILY",
                next_trigger_at=_NOW,
                status="deleted",
                description="bad",
            )

    async def test_invalid_notification_tier(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ScheduleRepo.create(
                db_session,
                user_id=user.telegram_id,
                schedule_type="daily_review",
                rrule="FREQ=DAILY",
                next_trigger_at=_NOW,
                notification_tier="gpt4",
                description="bad",
            )

    async def test_invalid_created_by(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await ScheduleRepo.create(
                db_session,
                user_id=user.telegram_id,
                schedule_type="daily_review",
                rrule="FREQ=DAILY",
                next_trigger_at=_NOW,
                created_by="hacker",
                description="bad",
            )


# ---------------------------------------------------------------------------
# recalculate_triggers_for_user (Fix #1)
# ---------------------------------------------------------------------------

class TestRecalculateTriggers:

    async def test_recalculate_triggers_does_not_crash(self, db_session: AsyncSession, make_user):
        """recalculate_triggers_for_user must succeed (previously crashed with AttributeError)."""
        user = await make_user(timezone="America/New_York")
        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            next_trigger_at=_NOW + timedelta(hours=1),
            description="test",
        )
        # This was crashing with AttributeError: ScheduleRepo.update
        updated = await ScheduleRepo.recalculate_triggers_for_user(
            db_session, user.telegram_id, "America/New_York",
        )
        assert updated == 1

    async def test_recalculate_updates_next_trigger_at(self, db_session: AsyncSession, make_user):
        """After timezone change, next_trigger_at should be recalculated."""
        user = await make_user(timezone="UTC")
        original_trigger = _NOW + timedelta(hours=1)
        sched = await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type="daily_review",
            rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            next_trigger_at=original_trigger,
            description="test",
        )
        await ScheduleRepo.recalculate_triggers_for_user(
            db_session, user.telegram_id, "Asia/Tokyo",
        )
        await db_session.refresh(sched)
        # Trigger time should have changed (different timezone)
        assert sched.next_trigger_at != original_trigger
