"""Integration tests for notification dispatch infrastructure.

Tests Redis-backed dedup, LLM counters, NotificationRepo with LLM fields,
and the should_send() gate function against real PostgreSQL and Redis.
"""

import contextlib
from datetime import date, time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from adaptive_lang_study_bot.cache.keys import NOTIF_DEDUP_KEY, NOTIF_LLM_KEY
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.db.repositories import NotificationRepo, UserRepo
from adaptive_lang_study_bot.enums import NotificationStatus

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures for should_send() (needs a committing session + monkeypatching)
# ---------------------------------------------------------------------------

_DISPATCH_USER_COUNTER = 500_000


@pytest.fixture()
async def dispatch_env(pg_url, redis_client, monkeypatch):
    """Provide a committing DB session and patch dispatcher dependencies.

    Unlike the transactional ``db_session`` fixture, should_send() opens its
    own session internally and calls commit().  We create a separate engine,
    patch ``dispatcher.async_session_factory`` to use it, and clean up via
    CASCADE delete after the test.
    """
    engine = create_async_engine(pg_url)

    @contextlib.asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(
        "adaptive_lang_study_bot.proactive.dispatcher.async_session_factory",
        _factory,
    )

    # Patch get_redis inside dispatcher module (import-time binding)
    async def _fake_get_redis():
        return redis_client

    monkeypatch.setattr(
        "adaptive_lang_study_bot.proactive.dispatcher.get_redis",
        _fake_get_redis,
    )

    # Setup session for creating test data
    setup = AsyncSession(bind=engine, expire_on_commit=False)
    yield setup

    # Cleanup
    from sqlalchemy import delete as sa_delete
    await setup.execute(sa_delete(User))
    await setup.commit()
    await setup.close()
    await engine.dispose()


@pytest.fixture()
def make_dispatch_user(dispatch_env):
    """Factory for creating users visible to should_send()."""

    async def _factory(**overrides) -> User:
        global _DISPATCH_USER_COUNTER
        _DISPATCH_USER_COUNTER += 1
        defaults = {
            "telegram_id": _DISPATCH_USER_COUNTER,
            "first_name": f"DispUser{_DISPATCH_USER_COUNTER}",
            "native_language": "en",
            "target_language": "es",
            "max_notifications_per_day": 3,
        }
        defaults.update(overrides)
        user = User(**defaults)
        dispatch_env.add(user)
        await dispatch_env.commit()
        return user

    return _factory


# ===========================================================================
# Redis notification dedup
# ===========================================================================

class TestNotificationDedup:

    async def test_dedup_key_blocks_duplicate(self, redis_client, mock_get_redis):
        """Setting a dedup key prevents the same notification type on the same day."""
        key = NOTIF_DEDUP_KEY.format(user_id=999, type="streak_risk", date="2026-02-22")
        await redis_client.set(key, "1", nx=True, ex=86400)
        assert await redis_client.exists(key) == 1

    async def test_dedup_key_nx_prevents_overwrite(self, redis_client, mock_get_redis):
        """SET NX should not overwrite an existing key."""
        key = NOTIF_DEDUP_KEY.format(user_id=999, type="cards_due", date="2026-02-22")
        result1 = await redis_client.set(key, "1", nx=True, ex=86400)
        result2 = await redis_client.set(key, "1", nx=True, ex=86400)
        assert result1 is True
        assert result2 is None  # NX prevents overwrite

    async def test_different_types_independent(self, redis_client, mock_get_redis):
        """Different notification types have independent dedup keys."""
        key1 = NOTIF_DEDUP_KEY.format(user_id=999, type="streak_risk", date="2026-02-22")
        key2 = NOTIF_DEDUP_KEY.format(user_id=999, type="cards_due", date="2026-02-22")
        await redis_client.set(key1, "1", nx=True, ex=86400)
        assert await redis_client.exists(key1) == 1
        assert await redis_client.exists(key2) == 0

    async def test_different_dates_independent(self, redis_client, mock_get_redis):
        """Same notification type on different dates has independent keys."""
        key1 = NOTIF_DEDUP_KEY.format(user_id=999, type="streak_risk", date="2026-02-22")
        key2 = NOTIF_DEDUP_KEY.format(user_id=999, type="streak_risk", date="2026-02-23")
        await redis_client.set(key1, "1", nx=True, ex=86400)
        assert await redis_client.exists(key1) == 1
        assert await redis_client.exists(key2) == 0

    async def test_different_users_independent(self, redis_client, mock_get_redis):
        """Different users have independent dedup keys."""
        key1 = NOTIF_DEDUP_KEY.format(user_id=100, type="streak_risk", date="2026-02-22")
        key2 = NOTIF_DEDUP_KEY.format(user_id=200, type="streak_risk", date="2026-02-22")
        await redis_client.set(key1, "1", nx=True, ex=86400)
        assert await redis_client.exists(key1) == 1
        assert await redis_client.exists(key2) == 0


# ===========================================================================
# Redis LLM notification counter
# ===========================================================================

class TestLLMNotificationCounter:

    async def test_incr_and_read(self, redis_client, mock_get_redis):
        """INCR creates the counter and increments it."""
        key = NOTIF_LLM_KEY.format(user_id=999, date="2026-02-22")
        await redis_client.incr(key)
        assert int(await redis_client.get(key)) == 1
        await redis_client.incr(key)
        assert int(await redis_client.get(key)) == 2

    async def test_pipeline_incr_and_expire(self, redis_client, mock_get_redis):
        """Pipeline INCR + EXPIRE works atomically."""
        key = NOTIF_LLM_KEY.format(user_id=999, date="2026-02-22")
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 86400)
            results = await pipe.execute()
        assert results[0] == 1  # INCR returns new value
        assert results[1] is True  # EXPIRE returns True
        ttl = await redis_client.ttl(key)
        assert 0 < ttl <= 86400

    async def test_counter_starts_at_zero(self, redis_client, mock_get_redis):
        """Non-existent counter reads as 0 (or None)."""
        key = NOTIF_LLM_KEY.format(user_id=999, date="2026-02-22")
        val = await redis_client.get(key)
        assert val is None
        assert int(val or 0) == 0

    async def test_different_users_independent(self, redis_client, mock_get_redis):
        """LLM counters are per-user."""
        key1 = NOTIF_LLM_KEY.format(user_id=100, date="2026-02-22")
        key2 = NOTIF_LLM_KEY.format(user_id=200, date="2026-02-22")
        await redis_client.incr(key1)
        await redis_client.incr(key1)
        await redis_client.incr(key2)
        assert int(await redis_client.get(key1)) == 2
        assert int(await redis_client.get(key2)) == 1

    async def test_different_dates_independent(self, redis_client, mock_get_redis):
        """LLM counters are per-date."""
        key1 = NOTIF_LLM_KEY.format(user_id=999, date="2026-02-22")
        key2 = NOTIF_LLM_KEY.format(user_id=999, date="2026-02-23")
        await redis_client.incr(key1)
        await redis_client.incr(key1)
        await redis_client.incr(key1)
        assert int(await redis_client.get(key1)) == 3
        val2 = await redis_client.get(key2)
        assert val2 is None


# ===========================================================================
# NotificationRepo with LLM tier fields
# ===========================================================================

class TestNotificationRepoLLMFields:

    async def test_create_llm_notification_with_cost(
        self, db_session: AsyncSession, make_user,
    ):
        """Creating an LLM-tier notification stores cost_usd."""
        user = await make_user()
        notif = await NotificationRepo.create(
            db_session,
            user_id=user.telegram_id,
            notification_type="streak_risk",
            tier="llm",
            trigger_source="event",
            message_text="LLM-generated message",
            cost_usd=0.002,
        )
        assert notif.tier == "llm"
        assert notif.cost_usd == pytest.approx(0.002)

    async def test_template_notification_zero_cost(
        self, db_session: AsyncSession, make_user,
    ):
        """Template-tier notifications have zero cost by default."""
        user = await make_user()
        notif = await NotificationRepo.create(
            db_session,
            user_id=user.telegram_id,
            notification_type="cards_due",
            tier="template",
            trigger_source="schedule",
            message_text="Template message",
        )
        assert notif.tier == "template"
        assert (notif.cost_usd or 0) == 0

    async def test_hybrid_notification_with_cost(
        self, db_session: AsyncSession, make_user,
    ):
        """Hybrid-tier notifications store cost."""
        user = await make_user()
        notif = await NotificationRepo.create(
            db_session,
            user_id=user.telegram_id,
            notification_type="user_inactive",
            tier="hybrid",
            trigger_source="event",
            message_text="Hybrid message",
            cost_usd=0.001,
        )
        assert notif.tier == "hybrid"
        assert notif.cost_usd == pytest.approx(0.001)

    async def test_get_recent_returns_llm_fields(
        self, db_session: AsyncSession, make_user,
    ):
        """get_recent includes tier and cost_usd fields."""
        user = await make_user()
        await NotificationRepo.create(
            db_session,
            user_id=user.telegram_id,
            notification_type="streak_risk",
            tier="llm",
            trigger_source="event",
            message_text="LLM msg",
            cost_usd=0.003,
        )
        recent = await NotificationRepo.get_recent(db_session, user.telegram_id)
        assert len(recent) == 1
        assert recent[0].tier == "llm"
        assert recent[0].cost_usd == pytest.approx(0.003)


# ===========================================================================
# should_send() with real DB + Redis
# ===========================================================================

class TestShouldSendIntegration:

    async def test_normal_user_can_send(
        self, dispatch_env, make_dispatch_user, redis_client,
    ):
        """Normal user with no restrictions passes all gates."""
        user = await make_dispatch_user()

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "streak_risk")
        assert can_send is True
        assert reason == ""

    async def test_paused_user_blocked(
        self, dispatch_env, make_dispatch_user, redis_client,
    ):
        """User with notifications_paused=True is blocked."""
        user = await make_dispatch_user(notifications_paused=True)

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_PAUSED

    async def test_daily_limit_not_checked_in_should_send(
        self, dispatch_env, make_dispatch_user, redis_client,
    ):
        """Daily limit is now enforced atomically in dispatch_notification(),
        not in should_send().  should_send() passes through to dedup check.
        """
        today = date.today()
        user = await make_dispatch_user(
            max_notifications_per_day=2,
            notifications_sent_today=2,
            notifications_count_reset_date=today,
        )

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "streak_risk")
        # should_send no longer checks daily limit — passes through
        assert can_send is True
        assert reason == ""

    async def test_dedup_blocks(
        self, dispatch_env, make_dispatch_user, redis_client,
    ):
        """Existing dedup key blocks the same notification type."""
        user = await make_dispatch_user()
        today_str = date.today().isoformat()
        dedup_key = NOTIF_DEDUP_KEY.format(
            user_id=user.telegram_id, type="streak_risk", date=today_str,
        )
        await redis_client.set(dedup_key, "1", nx=True, ex=86400)

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_DEDUP

    async def test_dedup_allows_different_type(
        self, dispatch_env, make_dispatch_user, redis_client,
    ):
        """Dedup for one type does not block a different type."""
        user = await make_dispatch_user()
        today_str = date.today().isoformat()
        dedup_key = NOTIF_DEDUP_KEY.format(
            user_id=user.telegram_id, type="streak_risk", date=today_str,
        )
        await redis_client.set(dedup_key, "1", nx=True, ex=86400)

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "cards_due")
        assert can_send is True

    async def test_quiet_hours_blocks(
        self, dispatch_env, make_dispatch_user, redis_client, monkeypatch,
    ):
        """User in quiet hours is blocked."""
        user = await make_dispatch_user(
            quiet_hours_start=time(0, 0),
            quiet_hours_end=time(23, 59),
        )

        from adaptive_lang_study_bot.proactive.dispatcher import should_send
        can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_QUIET
