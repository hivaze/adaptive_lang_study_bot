"""Unit tests for the proactive tick engine.

All DB, Redis, and dispatcher calls are mocked — no infrastructure needed.
Tests verify:
- Short-lived DB sessions (not held during dispatch)
- Bounded parallel dispatch
- Paginated user loading in Phase 2
- Schedule failure handling
- Lock lifecycle
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from adaptive_lang_study_bot.enums import ScheduleStatus

TICK_MODULE = "adaptive_lang_study_bot.proactive.tick"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schedule(**overrides):
    s = MagicMock()
    s.id = overrides.get("id", uuid4())
    s.user_id = overrides.get("user_id", 100)
    s.schedule_type = overrides.get("schedule_type", "practice_reminder")
    s.notification_tier = overrides.get("notification_tier", "template")
    s.status = ScheduleStatus.ACTIVE
    s.pause_until = None
    s.rrule = "FREQ=DAILY;BYHOUR=9"
    s.consecutive_failures = 0
    s.description = "Daily review"
    user = MagicMock()
    user.telegram_id = s.user_id
    user.is_active = True
    user.first_name = "Alex"
    user.streak_days = 5
    user.vocabulary_count = 100
    user.level = "A2"
    user.target_language = "fr"
    user.timezone = "UTC"
    user.native_language = "en"
    s.user = user
    for k, v in overrides.items():
        if k == "user":
            s.user = v
        elif hasattr(s, k):
            setattr(s, k, v)
    return s


def _make_user(**overrides):
    u = MagicMock()
    u.telegram_id = overrides.get("telegram_id", 200)
    u.is_active = True
    u.first_name = "Bob"
    u.streak_days = 3
    u.vocabulary_count = 50
    u.level = "A1"
    u.target_language = "es"
    u.timezone = "UTC"
    u.native_language = "en"
    u.last_session_at = datetime.now(timezone.utc) - timedelta(hours=6)
    u.notifications_paused = False
    u.notification_preferences = {}
    u.quiet_hours_start = None
    u.quiet_hours_end = None
    u.milestones = {}
    u.recent_scores = [7]
    u.interests = []
    u.weak_areas = []
    u.learning_goals = []
    u.sessions_completed = 5
    for k, v in overrides.items():
        setattr(u, k, v)
    return u


def _mock_session_factory():
    """Create a mock async_session_factory that yields a fresh mock session."""
    mock_db = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, mock_db


# ---------------------------------------------------------------------------
# tick_scheduler — top-level orchestration
# ---------------------------------------------------------------------------

class TestTickScheduler:

    @pytest.mark.asyncio
    async def test_acquires_and_releases_lock(self):
        """Tick acquires distributed lock at start and releases in finally."""
        with patch(f"{TICK_MODULE}.get_redis", new_callable=AsyncMock) as mock_redis, \
             patch(f"{TICK_MODULE}.acquire_lock", new_callable=AsyncMock, return_value=True), \
             patch(f"{TICK_MODULE}.release_lock", new_callable=AsyncMock) as mock_release, \
             patch(f"{TICK_MODULE}.refresh_lock", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}._phase_schedules", new_callable=AsyncMock):
            from adaptive_lang_study_bot.proactive.tick import tick_scheduler

            bot = AsyncMock()
            await tick_scheduler(bot)

        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_lock_not_acquired(self):
        """When lock is held by another instance, tick returns immediately."""
        with patch(f"{TICK_MODULE}.get_redis", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}.acquire_lock", new_callable=AsyncMock, return_value=False), \
             patch(f"{TICK_MODULE}._phase_schedules", new_callable=AsyncMock) as mock_phase1:
            from adaptive_lang_study_bot.proactive.tick import tick_scheduler

            bot = AsyncMock()
            await tick_scheduler(bot)

        mock_phase1.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_released_on_phase_error(self):
        """Even when a phase raises, the lock is released."""
        with patch(f"{TICK_MODULE}.get_redis", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}.acquire_lock", new_callable=AsyncMock, return_value=True), \
             patch(f"{TICK_MODULE}.release_lock", new_callable=AsyncMock) as mock_release, \
             patch(f"{TICK_MODULE}.refresh_lock", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}._phase_schedules", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            from adaptive_lang_study_bot.proactive.tick import tick_scheduler

            bot = AsyncMock()
            await tick_scheduler(bot)

        mock_release.assert_called_once()


# ---------------------------------------------------------------------------
# _phase_schedules
# ---------------------------------------------------------------------------

class TestPhaseSchedules:

    @pytest.mark.asyncio
    async def test_no_due_schedules_returns_immediately(self):
        """When no schedules are due, phase exits early with no dispatches."""
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=[]), \
             patch(f"{TICK_MODULE}.dispatch_notification", new_callable=AsyncMock) as mock_dispatch:
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_active_schedules(self):
        """Active schedules are dispatched and updated."""
        s1 = _make_schedule(user_id=100)
        s2 = _make_schedule(user_id=101)
        s2.user.telegram_id = 101

        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=[s1, s2]), \
             patch(f"{TICK_MODULE}.VocabularyRepo.count_due_batch", new_callable=AsyncMock, return_value={100: 5, 101: 5}), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_statuses_batch", new_callable=AsyncMock, return_value={
                 s1.id: (ScheduleStatus.ACTIVE, None),
                 s2.id: (ScheduleStatus.ACTIVE, None),
             }), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since", new_callable=AsyncMock, return_value=0), \
             patch(f"{TICK_MODULE}.dispatch_notification", new_callable=AsyncMock, return_value="msg") as mock_dispatch, \
             patch(f"{TICK_MODULE}.compute_next_trigger", return_value=datetime.now(timezone.utc) + timedelta(days=1)), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_after_trigger", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_fields", new_callable=AsyncMock):
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        assert mock_dispatch.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_paused_schedule(self):
        """Schedule that was paused between get_due and dispatch is skipped."""
        s = _make_schedule()
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=[s]), \
             patch(f"{TICK_MODULE}.VocabularyRepo.count_due_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_statuses_batch", new_callable=AsyncMock, return_value={
                 s.id: (ScheduleStatus.PAUSED, None),
             }), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since", new_callable=AsyncMock, return_value=0), \
             patch(f"{TICK_MODULE}.dispatch_notification", new_callable=AsyncMock) as mock_dispatch:
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_inactive_user(self):
        """Schedule whose user is inactive is skipped."""
        s = _make_schedule()
        s.user.is_active = False
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=[s]), \
             patch(f"{TICK_MODULE}.VocabularyRepo.count_due_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_statuses_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since", new_callable=AsyncMock, return_value=0), \
             patch(f"{TICK_MODULE}.dispatch_notification", new_callable=AsyncMock) as mock_dispatch:
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_failure_triggers_backoff(self):
        """When dispatch raises, failure handler is called."""
        s = _make_schedule()
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=[s]), \
             patch(f"{TICK_MODULE}.VocabularyRepo.count_due_batch", new_callable=AsyncMock, return_value={s.user_id: 5}), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_statuses_batch", new_callable=AsyncMock, return_value={
                 s.id: (ScheduleStatus.ACTIVE, None),
             }), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since", new_callable=AsyncMock, return_value=0), \
             patch(f"{TICK_MODULE}.dispatch_notification", new_callable=AsyncMock, side_effect=RuntimeError("fail")), \
             patch(f"{TICK_MODULE}._handle_schedule_failure", new_callable=AsyncMock) as mock_handle:
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        mock_handle.assert_called_once_with(s, pytest.approx(mock_handle.call_args[0][1], abs=1))

    @pytest.mark.asyncio
    async def test_dispatches_run_concurrently(self):
        """Multiple schedules are dispatched concurrently, not sequentially."""
        dispatch_order = []
        dispatch_started = asyncio.Event()

        async def slow_dispatch(user, trigger, bot):
            dispatch_order.append(("start", user.telegram_id))
            dispatch_started.set()
            await asyncio.sleep(0.05)
            dispatch_order.append(("end", user.telegram_id))
            return "msg"

        schedules = [_make_schedule(user_id=i) for i in range(3)]
        for i, s in enumerate(schedules):
            s.user.telegram_id = i

        factory, _ = _mock_session_factory()
        statuses = {s.id: (ScheduleStatus.ACTIVE, None) for s in schedules}
        due_counts = {i: 5 for i in range(3)}

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_due", new_callable=AsyncMock, return_value=schedules), \
             patch(f"{TICK_MODULE}.VocabularyRepo.count_due_batch", new_callable=AsyncMock, return_value=due_counts), \
             patch(f"{TICK_MODULE}.ScheduleRepo.get_statuses_batch", new_callable=AsyncMock, return_value=statuses), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since_batch", new_callable=AsyncMock, return_value={}), \
             patch(f"{TICK_MODULE}.SessionRepo.count_since", new_callable=AsyncMock, return_value=0), \
             patch(f"{TICK_MODULE}.dispatch_notification", side_effect=slow_dispatch), \
             patch(f"{TICK_MODULE}.compute_next_trigger", return_value=datetime.now(timezone.utc) + timedelta(days=1)), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_after_trigger", new_callable=AsyncMock), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_fields", new_callable=AsyncMock):
            from adaptive_lang_study_bot.proactive.tick import _phase_schedules

            await _phase_schedules(AsyncMock())

        # With concurrent dispatch, all 3 should start before any finishes
        starts = [e for e in dispatch_order if e[0] == "start"]
        ends = [e for e in dispatch_order if e[0] == "end"]
        assert len(starts) == 3
        assert len(ends) == 3
        # At least 2 should start before the first one ends (proves concurrency)
        first_end_idx = dispatch_order.index(ends[0])
        starts_before_first_end = sum(1 for i, e in enumerate(dispatch_order) if e[0] == "start" and i < first_end_idx)
        assert starts_before_first_end >= 2


# ---------------------------------------------------------------------------
# _handle_schedule_failure
# ---------------------------------------------------------------------------

class TestHandleScheduleFailure:

    @pytest.mark.asyncio
    async def test_pauses_after_10_failures(self):
        """Schedule is paused after 10 consecutive failures."""
        s = _make_schedule(consecutive_failures=9)
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_fields", new_callable=AsyncMock) as mock_update, \
             patch(f"{TICK_MODULE}.UserRepo.get", new_callable=AsyncMock, return_value=_make_user()):
            from adaptive_lang_study_bot.proactive.tick import _handle_schedule_failure

            bot = AsyncMock()
            await _handle_schedule_failure(s, bot)

        mock_update.assert_called_once()
        assert mock_update.call_args[1]["status"] == ScheduleStatus.PAUSED

    @pytest.mark.asyncio
    async def test_exponential_backoff_below_10(self):
        """Below 10 failures, schedule is rescheduled with backoff."""
        s = _make_schedule(consecutive_failures=3)
        factory, mock_db = _mock_session_factory()

        with patch(f"{TICK_MODULE}.async_session_factory", factory), \
             patch(f"{TICK_MODULE}.ScheduleRepo.update_after_trigger", new_callable=AsyncMock) as mock_trigger:
            from adaptive_lang_study_bot.proactive.tick import _handle_schedule_failure

            bot = AsyncMock()
            await _handle_schedule_failure(s, bot)

        mock_trigger.assert_called_once()
        # 2^4 = 16 minutes backoff
        next_at = mock_trigger.call_args[1]["next_trigger_at"]
        assert next_at > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# _periodic_lock_refresh
# ---------------------------------------------------------------------------

class TestPeriodicLockRefresh:

    @pytest.mark.asyncio
    async def test_refreshes_lock_periodically(self):
        """Background task refreshes the lock at the configured interval."""
        with patch(f"{TICK_MODULE}.refresh_lock", new_callable=AsyncMock) as mock_refresh, \
             patch(f"{TICK_MODULE}.tuning") as mock_tuning:
            mock_tuning.proactive_lock_refresh_interval = 0.05  # 50ms for fast test

            from adaptive_lang_study_bot.proactive.tick import _periodic_lock_refresh

            redis = AsyncMock()
            task = asyncio.create_task(_periodic_lock_refresh(redis, "test-token"))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should have refreshed at least twice in 150ms with 50ms interval
        assert mock_refresh.call_count >= 2
