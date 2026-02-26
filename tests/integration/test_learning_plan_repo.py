"""Integration tests for LearningPlanRepo and ExerciseResultRepo.get_stats_for_topics."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import LearningPlan
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    LearningPlanRepo,
    UserRepo,
)

pytestmark = pytest.mark.integration


def _plan_defaults(user_id: int, **overrides) -> dict:
    """Build kwargs for LearningPlanRepo.create with sensible defaults."""
    base = {
        "user_id": user_id,
        "current_level": "A2",
        "target_level": "B1",
        "start_date": date(2026, 3, 1),
        "target_end_date": date(2026, 3, 28),
        "total_weeks": 4,
        "plan_data": {
            "description": "A2 to B1 plan",
            "phases": [
                {
                    "week": 1,
                    "focus": "Past tense foundations",
                    "topics": ["Past tense review", "Conditional mood"],
                    "vocabulary_target": 15,
                },
                {
                    "week": 2,
                    "focus": "Subjunctive basics",
                    "topics": ["Subjunctive introduction"],
                    "vocabulary_target": 10,
                },
            ],
        },
    }
    base.update(overrides)
    return base


# ===========================================================================
# LearningPlanRepo CRUD
# ===========================================================================


class TestLearningPlanCRUD:

    async def test_create_and_get_active(self, db_session: AsyncSession, make_user):
        user = await make_user()
        plan = await LearningPlanRepo.create(db_session, **_plan_defaults(user.telegram_id))
        assert plan.id is not None

        active = await LearningPlanRepo.get_active(db_session, user.telegram_id)
        assert active is not None
        assert active.id == plan.id

    async def test_get_active_returns_none_when_no_plan(self, db_session: AsyncSession, make_user):
        user = await make_user()
        active = await LearningPlanRepo.get_active(db_session, user.telegram_id)
        assert active is None

    async def test_create_replaces_existing(self, db_session: AsyncSession, make_user):
        """Creating a new plan deletes any existing plan for the user."""
        user = await make_user()
        plan1 = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user.telegram_id),
        )
        plan1_id = plan1.id

        plan2 = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user.telegram_id),
        )

        active = await LearningPlanRepo.get_active(db_session, user.telegram_id)
        assert active.id == plan2.id

        # Old plan should be gone
        await db_session.flush()
        db_session.expire_all()
        result = await db_session.execute(
            select(LearningPlan).where(LearningPlan.id == plan1_id),
        )
        assert result.scalar_one_or_none() is None

    async def test_update_fields(self, db_session: AsyncSession, make_user):
        user = await make_user()
        plan = await LearningPlanRepo.create(db_session, **_plan_defaults(user.telegram_id))
        plan_id = plan.id

        await LearningPlanRepo.update_fields(
            db_session, plan_id, total_weeks=6,
            target_end_date=date(2026, 4, 11),
        )
        await db_session.flush()
        db_session.expire_all()

        result = await db_session.execute(
            select(LearningPlan).where(LearningPlan.id == plan_id),
        )
        updated = result.scalar_one()
        assert updated.total_weeks == 6
        assert updated.target_end_date == date(2026, 4, 11)

    async def test_delete(self, db_session: AsyncSession, make_user):
        user = await make_user()
        plan = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user.telegram_id),
        )
        plan_id = plan.id

        await LearningPlanRepo.delete(db_session, user.telegram_id)
        await db_session.flush()
        db_session.expire_all()

        result = await db_session.execute(
            select(LearningPlan).where(LearningPlan.id == plan_id),
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_noop_when_no_plan(self, db_session: AsyncSession, make_user):
        """Deleting when no plan exists should be a no-op (no error)."""
        user = await make_user()
        await LearningPlanRepo.delete(db_session, user.telegram_id)

    async def test_plan_data_stored_as_jsonb(self, db_session: AsyncSession, make_user):
        user = await make_user()
        plan = await LearningPlanRepo.create(db_session, **_plan_defaults(user.telegram_id))
        assert isinstance(plan.plan_data, dict)
        assert "phases" in plan.plan_data
        assert len(plan.plan_data["phases"]) == 2


# ===========================================================================
# Constraints
# ===========================================================================


class TestLearningPlanConstraints:

    async def test_different_users_can_have_plans(self, db_session: AsyncSession, make_user):
        user1 = await make_user()
        user2 = await make_user()
        p1 = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user1.telegram_id),
        )
        p2 = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user2.telegram_id),
        )
        assert p1.id != p2.id

    async def test_invalid_level_rejected(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await LearningPlanRepo.create(
                db_session, **_plan_defaults(user.telegram_id, current_level="X1"),
            )


# ===========================================================================
# Cascade delete
# ===========================================================================


class TestLearningPlanCascade:

    async def test_delete_user_cascades_plans(self, db_session: AsyncSession, make_user):
        user = await make_user()
        plan = await LearningPlanRepo.create(
            db_session, **_plan_defaults(user.telegram_id),
        )
        plan_id = plan.id

        await UserRepo.delete(db_session, user.telegram_id)
        await db_session.flush()
        db_session.expire_all()

        result = await db_session.execute(
            select(LearningPlan).where(LearningPlan.id == plan_id),
        )
        assert result.scalar_one_or_none() is None


# ===========================================================================
# ExerciseResultRepo.get_stats_for_topics
# ===========================================================================


class TestGetStatsForTopics:

    async def test_basic_stats(self, db_session: AsyncSession, make_user):
        user = await make_user()
        for score in [6, 8, 10]:
            await ExerciseResultRepo.create(
                db_session, user_id=user.telegram_id,
                exercise_type="fill_blank", topic="Past tense review", score=score,
            )
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Past tense review"],
            date(2026, 1, 1),
        )
        assert "Past tense review" in stats
        assert stats["Past tense review"]["count"] == 3
        assert stats["Past tense review"]["avg_score"] == pytest.approx(8.0)

    async def test_case_insensitive_matching(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="past tense review", score=7,
        )
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Past Tense Review"],  # different casing
            date(2026, 1, 1),
        )
        # Should map back to the plan topic name
        assert "Past Tense Review" in stats
        assert stats["Past Tense Review"]["count"] == 1

    async def test_empty_topics_returns_empty(self, db_session: AsyncSession, make_user):
        user = await make_user()
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id, [], date(2026, 1, 1),
        )
        assert stats == {}

    async def test_no_matching_exercises(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="unrelated topic", score=9,
        )
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Past tense review"],
            date(2026, 1, 1),
        )
        assert "Past tense review" not in stats

    async def test_since_filter(self, db_session: AsyncSession, make_user):
        """Only exercises created after `since` date are included."""
        user = await make_user()
        # Create two exercises — one old, one recent
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="Verbs", score=5,
        )
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Verbs"],
            date(2026, 1, 1),
        )
        assert stats["Verbs"]["count"] == 1

        # With a future since date — should return nothing
        stats_future = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Verbs"],
            date(2099, 1, 1),
        )
        assert "Verbs" not in stats_future

    async def test_multiple_topics(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="Grammar A", score=8,
        )
        await ExerciseResultRepo.create(
            db_session, user_id=user.telegram_id,
            exercise_type="fill_blank", topic="Grammar B", score=6,
        )
        stats = await ExerciseResultRepo.get_stats_for_topics(
            db_session, user.telegram_id,
            ["Grammar A", "Grammar B", "Grammar C"],
            date(2026, 1, 1),
        )
        assert "Grammar A" in stats
        assert "Grammar B" in stats
        assert "Grammar C" not in stats
