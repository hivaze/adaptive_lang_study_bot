"""Integration tests for SessionRepo against real PostgreSQL."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.db.models import Session as SessionModel
from adaptive_lang_study_bot.db.repositories import SessionRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------

class TestSessionCRUD:

    async def test_create_and_get(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="interactive",
        )
        assert sess.id is not None
        assert isinstance(sess.id, uuid.UUID)

        fetched = await SessionRepo.get(db_session, sess.id)
        assert fetched is not None
        assert fetched.session_type == "interactive"
        assert fetched.cost_usd == 0
        assert fetched.pipeline_status == "pending"
        assert fetched.ended_at is None


# ---------------------------------------------------------------------------
# update_end
# ---------------------------------------------------------------------------

class TestUpdateEnd:

    async def test_update_end_sets_all_fields(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="interactive",
        )
        await SessionRepo.update_end(
            db_session, sess.id,
            cost_usd=0.05,
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=100,
            num_turns=5,
            tool_calls_count=3,
            tool_calls_detail={"get_user_profile": 1, "add_vocabulary": 2},
            duration_ms=15000,
        )
        await db_session.refresh(sess)

        assert sess.ended_at is not None
        assert float(sess.cost_usd) == pytest.approx(0.05)
        assert sess.input_tokens == 1000
        assert sess.output_tokens == 500
        assert sess.num_turns == 5
        assert sess.tool_calls_count == 3
        assert sess.tool_calls_detail == {"get_user_profile": 1, "add_vocabulary": 2}
        assert sess.duration_ms == 15000


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------

class TestPipelineStatus:

    async def test_set_pipeline_status(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="interactive",
        )
        await SessionRepo.set_pipeline_status(
            db_session, sess.id, "completed",
        )
        await db_session.refresh(sess)
        assert sess.pipeline_status == "completed"

    async def test_set_pipeline_status_with_issues(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="interactive",
        )
        issues = {"warnings": ["missed_tool_call"]}
        await SessionRepo.set_pipeline_status(
            db_session, sess.id, "failed", issues=issues,
        )
        await db_session.refresh(sess)
        assert sess.pipeline_status == "failed"
        assert sess.pipeline_issues == issues


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

class TestSessionQueries:

    async def test_get_recent_ordering(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s1 = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        s2 = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        recent = await SessionRepo.get_recent(db_session, user.telegram_id, limit=10)
        # Most recent first
        assert recent[0].id == s2.id

    async def test_count_today(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="proactive_review",
        )

        count = await SessionRepo.count_today(db_session, user.telegram_id)
        assert count == 1  # only interactive

    async def test_count_today_all(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="proactive_review",
        )

        count = await SessionRepo.count_today_all(db_session)
        assert count >= 2  # at least our 2

    async def test_get_total_cost_today(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.12)

        total = await SessionRepo.get_total_cost_today(db_session, user.telegram_id)
        assert total == pytest.approx(0.12)

    async def test_get_daily_cost(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.25)

        cost = await SessionRepo.get_daily_cost(db_session, date.today())
        assert cost >= 0.25

    async def test_get_cost_per_user(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.50)

        results = await SessionRepo.get_cost_per_user(db_session, days=7)
        assert isinstance(results, list)
        # Find our user
        our = [r for r in results if r[0] == user.telegram_id]
        assert len(our) == 1
        assert float(our[0][2]) == pytest.approx(0.50)  # total_cost
        assert our[0][3] == 1  # session_count

    async def test_get_pipeline_failures(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s1 = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        s2 = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.set_pipeline_status(db_session, s1.id, "failed")
        await SessionRepo.set_pipeline_status(db_session, s2.id, "completed")

        failures = await SessionRepo.get_pipeline_failures(db_session)
        failure_ids = [s.id for s in failures]
        assert s1.id in failure_ids
        assert s2.id not in failure_ids

    async def test_count_pipeline_failures_recent(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.set_pipeline_status(db_session, s.id, "failed")

        count = await SessionRepo.count_pipeline_failures_recent(db_session, hours=1)
        assert count >= 1

    async def test_get_session_type_counts(self, db_session: AsyncSession, make_user):
        user = await make_user()
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="onboarding",
        )

        counts = await SessionRepo.get_session_type_counts(db_session, days=7)
        assert counts.get("interactive", 0) >= 1
        assert counts.get("onboarding", 0) >= 1

    async def test_get_total_cost_range(self, db_session: AsyncSession, make_user):
        user = await make_user()
        s = await SessionRepo.create(
            db_session, user_id=user.telegram_id, session_type="interactive",
        )
        await SessionRepo.update_end(db_session, s.id, cost_usd=0.10)

        today = date.today()
        total = await SessionRepo.get_total_cost_range(
            db_session, today - timedelta(days=1), today,
        )
        assert total >= 0.10


# ---------------------------------------------------------------------------
# Batch queries
# ---------------------------------------------------------------------------

class TestCountSinceBatch:

    async def test_returns_counts_per_user(self, db_session: AsyncSession, make_user):
        u1 = await make_user()
        u2 = await make_user()
        since = datetime.now(timezone.utc) - timedelta(hours=1)

        # Create sessions: 2 interactive for u1, 1 for u2, 1 proactive for u1
        await SessionRepo.create(db_session, user_id=u1.telegram_id, session_type="interactive")
        await SessionRepo.create(db_session, user_id=u1.telegram_id, session_type="interactive")
        await SessionRepo.create(db_session, user_id=u1.telegram_id, session_type="proactive_review")
        await SessionRepo.create(db_session, user_id=u2.telegram_id, session_type="interactive")

        result = await SessionRepo.count_since_batch(
            db_session, [u1.telegram_id, u2.telegram_id], since,
        )
        assert result[u1.telegram_id] == 2  # only interactive
        assert result[u2.telegram_id] == 1

    async def test_empty_user_ids_returns_empty(self, db_session: AsyncSession):
        result = await SessionRepo.count_since_batch(db_session, [], datetime.now(timezone.utc))
        assert result == {}

    async def test_users_with_no_sessions_excluded(self, db_session: AsyncSession, make_user):
        u1 = await make_user()
        since = datetime.now(timezone.utc) - timedelta(hours=1)

        result = await SessionRepo.count_since_batch(
            db_session, [u1.telegram_id], since,
        )
        # User with 0 sessions won't appear in the grouped result
        assert u1.telegram_id not in result


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------

class TestSessionCheckConstraints:

    async def test_invalid_session_type(self, db_session: AsyncSession, make_user):
        user = await make_user()
        with pytest.raises(IntegrityError):
            await SessionRepo.create(
                db_session,
                user_id=user.telegram_id,
                session_type="invalid_type",
            )

    async def test_invalid_pipeline_status(self, db_session: AsyncSession, make_user):
        user = await make_user()
        sess = await SessionRepo.create(
            db_session,
            user_id=user.telegram_id,
            session_type="interactive",
        )
        with pytest.raises(IntegrityError):
            await SessionRepo.set_pipeline_status(db_session, sess.id, "invalid")
            await db_session.flush()
