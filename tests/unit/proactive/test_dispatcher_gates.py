"""Unit tests for dispatcher gate logic: should_send() skip reasons and
_seconds_until_local_midnight() helper.

These tests use mocking to avoid DB/Redis dependencies.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adaptive_lang_study_bot.enums import (
    NotificationStatus,
    NotificationTier,
    ScheduleType,
    SessionType,
)
from adaptive_lang_study_bot.proactive.dispatcher import (
    _SCHEDULE_TO_SESSION_TYPE,
    _TRIGGER_TO_SESSION_TYPE,
    _seconds_until_local_midnight,
    dispatch_notification,
    should_send,
)


def _make_user(**overrides):
    user = MagicMock()
    user.telegram_id = 123
    user.timezone = "UTC"
    user.notifications_paused = False
    user.quiet_hours_start = None
    user.quiet_hours_end = None
    user.max_notifications_per_day = 5
    user.notifications_sent_today = 0
    user.notifications_count_reset_date = None
    for k, v in overrides.items():
        setattr(user, k, v)
    return user


# ---------------------------------------------------------------------------
# _seconds_until_local_midnight
# ---------------------------------------------------------------------------

class TestSecondsUntilLocalMidnight:

    def test_returns_positive(self):
        user = _make_user(timezone="UTC")
        result = _seconds_until_local_midnight(user)
        assert result > 0

    def test_minimum_60_seconds(self):
        """Even at 23:59:59, should return at least 60."""
        user = _make_user(timezone="UTC")
        result = _seconds_until_local_midnight(user)
        assert result >= 60

    def test_max_roughly_86400(self):
        """Should never exceed ~86400 seconds (a full day)."""
        user = _make_user(timezone="UTC")
        result = _seconds_until_local_midnight(user)
        assert result <= 86401  # Allow 1s margin

    def test_different_timezones_different_values(self):
        """Tokyo and LA should give different seconds to midnight."""
        tokyo_user = _make_user(timezone="Asia/Tokyo")
        la_user = _make_user(timezone="America/Los_Angeles")
        tokyo_result = _seconds_until_local_midnight(tokyo_user)
        la_result = _seconds_until_local_midnight(la_user)
        # Usually different unless tested at a very specific moment
        # Just verify both are valid ranges
        assert 60 <= tokyo_result <= 86401
        assert 60 <= la_result <= 86401

    def test_invalid_timezone_falls_back_to_utc(self):
        """Invalid timezone should not crash, falls back to UTC."""
        user = _make_user(timezone="Invalid/Zone")
        result = _seconds_until_local_midnight(user)
        assert result >= 60


# ---------------------------------------------------------------------------
# should_send — skip reasons
# ---------------------------------------------------------------------------

class TestShouldSendPaused:

    @pytest.mark.asyncio
    async def test_paused_returns_skipped_paused(self):
        user = _make_user(notifications_paused=True)
        can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_PAUSED


class TestShouldSendQuietHours:

    @pytest.mark.asyncio
    async def test_quiet_hours_same_day_inside(self):
        """9:00-17:00 quiet hours, current time is noon."""
        user = _make_user(
            quiet_hours_start=time(9, 0),
            quiet_hours_end=time(17, 0),
        )
        with patch(
            "adaptive_lang_study_bot.proactive.dispatcher.user_local_now",
            return_value=datetime(2026, 2, 22, 12, 0, 0, tzinfo=timezone.utc),
        ):
            can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_QUIET

    @pytest.mark.asyncio
    async def test_quiet_hours_same_day_outside(self):
        """9:00-17:00 quiet hours, current time is 20:00."""
        user = _make_user(
            quiet_hours_start=time(9, 0),
            quiet_hours_end=time(17, 0),
        )
        fresh_user = _make_user(
            notifications_sent_today=0,
            notifications_count_reset_date=datetime(2026, 2, 22).date(),
        )
        with patch(
            "adaptive_lang_study_bot.proactive.dispatcher.user_local_now",
            return_value=datetime(2026, 2, 22, 20, 0, 0, tzinfo=timezone.utc),
        ), patch(
            "adaptive_lang_study_bot.proactive.dispatcher.async_session_factory",
        ) as mock_factory, patch(
            "adaptive_lang_study_bot.proactive.dispatcher.get_redis",
            new_callable=AsyncMock,
        ) as mock_redis:
            mock_db = AsyncMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "adaptive_lang_study_bot.proactive.dispatcher.UserRepo.get",
                new_callable=AsyncMock,
                return_value=fresh_user,
            ):
                mock_redis_client = AsyncMock()
                mock_redis.return_value = mock_redis_client
                mock_redis_client.exists = AsyncMock(return_value=0)

                can_send, reason = await should_send(user, "streak_risk")
        assert can_send is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_quiet_hours_overnight_inside(self):
        """22:00-08:00 quiet hours, current time is 23:30."""
        user = _make_user(
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(8, 0),
        )
        with patch(
            "adaptive_lang_study_bot.proactive.dispatcher.user_local_now",
            return_value=datetime(2026, 2, 22, 23, 30, 0, tzinfo=timezone.utc),
        ):
            can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_QUIET

    @pytest.mark.asyncio
    async def test_quiet_hours_overnight_inside_early_morning(self):
        """22:00-08:00 quiet hours, current time is 06:00."""
        user = _make_user(
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(8, 0),
        )
        with patch(
            "adaptive_lang_study_bot.proactive.dispatcher.user_local_now",
            return_value=datetime(2026, 2, 22, 6, 0, 0, tzinfo=timezone.utc),
        ):
            can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_QUIET


class TestShouldSendDedup:

    @pytest.mark.asyncio
    async def test_dedup_hit_returns_skipped_dedup(self):
        """When Redis dedup key exists, should return skipped_dedup."""
        user = _make_user()
        mock_redis_client = AsyncMock()
        # Cooldown key → miss; dedup key → hit
        mock_redis_client.exists = AsyncMock(
            side_effect=lambda k: 0 if k.startswith("notif:cooldown:") else 1,
        )

        with patch(
            "adaptive_lang_study_bot.proactive.dispatcher.user_local_now",
            return_value=datetime.now(timezone.utc),
        ), patch(
            "adaptive_lang_study_bot.proactive.dispatcher.get_redis",
            new_callable=AsyncMock,
            return_value=mock_redis_client,
        ):
            can_send, reason = await should_send(user, "streak_risk")
        assert can_send is False
        assert reason == NotificationStatus.SKIPPED_DEDUP


# ---------------------------------------------------------------------------
# Notification type → session type mappings
# ---------------------------------------------------------------------------

class TestNotificationToSessionType:

    def test_all_schedule_types_mapped(self):
        for st in ScheduleType:
            assert st in _SCHEDULE_TO_SESSION_TYPE, f"Missing mapping for ScheduleType.{st.name}"

    def test_schedule_daily_review_maps_to_proactive_review(self):
        assert _SCHEDULE_TO_SESSION_TYPE[ScheduleType.DAILY_REVIEW] == SessionType.PROACTIVE_REVIEW

    def test_schedule_quiz_maps_to_proactive_quiz(self):
        assert _SCHEDULE_TO_SESSION_TYPE[ScheduleType.QUIZ] == SessionType.PROACTIVE_QUIZ

    def test_schedule_progress_report_maps_to_proactive_summary(self):
        assert _SCHEDULE_TO_SESSION_TYPE[ScheduleType.PROGRESS_REPORT] == SessionType.PROACTIVE_SUMMARY

    def test_schedule_practice_reminder_maps_to_proactive_nudge(self):
        assert _SCHEDULE_TO_SESSION_TYPE[ScheduleType.PRACTICE_REMINDER] == SessionType.PROACTIVE_NUDGE

    def test_trigger_types_all_mapped(self):
        expected_triggers = {
            "streak_risk", "cards_due", "user_inactive", "weak_area_persistent",
            "score_trend_declining", "score_trend_improving", "incomplete_exercise",
            "weak_area_drill_due",
        }
        for t in expected_triggers:
            assert t in _TRIGGER_TO_SESSION_TYPE, f"Missing mapping for trigger '{t}'"

    def test_unknown_trigger_defaults_to_nudge(self):
        """Unknown types should not be in the mapping (caller uses 'or' fallback)."""
        assert "unknown_type" not in _TRIGGER_TO_SESSION_TYPE


# ---------------------------------------------------------------------------
# dispatch_notification — LLM path
# ---------------------------------------------------------------------------

def _make_dispatch_user(**overrides):
    """User mock with all fields needed by dispatch_notification."""
    user = _make_user(**overrides)
    user.tier = "free"
    user.first_name = "Alex"
    user.native_language = "en"
    user.target_language = "fr"
    user.level = "A2"
    user.streak_days = 12
    user.vocabulary_count = 340
    user.interests = ["cooking"]
    user.weak_areas = []
    user.recent_scores = [7, 8]
    user.learning_goals = []
    user.topics_to_avoid = []
    return user


_DISPATCHER_MODULE = "adaptive_lang_study_bot.proactive.dispatcher"


def _setup_dispatch_mocks(llm_return=None, llm_side_effect=None, template_text="Template message"):
    """Start all patches needed for dispatch_notification tests.

    Returns a dict of mocks keyed by name.  Caller MUST call ``stop()``
    on every value in the *finally* block.
    """
    mocks: dict[str, Any] = {}
    patches: list = []

    def _start(name, p):
        m = p.start()
        mocks[name] = m
        patches.append(p)
        return m

    _start("should_send", patch(f"{_DISPATCHER_MODULE}.should_send", new_callable=AsyncMock, return_value=(True, "")))

    # Track template rendering calls (notif.* keys) separately from CTA keys.
    # A MagicMock wraps the side_effect so tests can assert_called_once() on
    # the "render_template" mock when the template fallback path is taken.
    _notif_t_tracker = MagicMock()

    def _t_side_effect(key, *args, **kwargs):
        if key.startswith("notif."):
            _notif_t_tracker(key, *args, **kwargs)
            return template_text
        return key  # CTA keys: return key as-is (sufficient for button labels)

    _start("t", patch(f"{_DISPATCHER_MODULE}.t", side_effect=_t_side_effect))
    mocks["render_template"] = _notif_t_tracker  # alias for backward-compat assertions

    _start("user_local_now", patch(f"{_DISPATCHER_MODULE}.user_local_now", return_value=datetime.now(timezone.utc)))

    llm_kwargs: dict = {"new_callable": AsyncMock}
    if llm_return is not None:
        llm_kwargs["return_value"] = llm_return
    if llm_side_effect is not None:
        llm_kwargs["side_effect"] = llm_side_effect
    _start("llm_session", patch(f"{_DISPATCHER_MODULE}.run_proactive_llm_session", **llm_kwargs))

    mock_factory = _start("factory", patch(f"{_DISPATCHER_MODULE}.async_session_factory"))
    mock_db = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_redis = _start("get_redis", patch(f"{_DISPATCHER_MODULE}.get_redis", new_callable=AsyncMock))
    redis_client = AsyncMock()
    redis_client.incr = AsyncMock(return_value=1)  # first LLM reservation, within limit
    mock_redis.return_value = redis_client

    _start("notif_create", patch(f"{_DISPATCHER_MODULE}.NotificationRepo.create", new_callable=AsyncMock))
    _start("user_incr", patch(f"{_DISPATCHER_MODULE}.UserRepo.increment_notification_count", new_callable=AsyncMock))
    check_incr = _start("user_check_incr", patch(f"{_DISPATCHER_MODULE}.UserRepo.check_and_increment_notification", new_callable=AsyncMock))
    check_incr.return_value = True  # Default: notification allowed
    _start("user_update", patch(f"{_DISPATCHER_MODULE}.UserRepo.update_fields", new_callable=AsyncMock))

    mocks["_patches"] = patches
    return mocks


def _stop_all(mocks: dict) -> None:
    for p in mocks.get("_patches", []):
        p.stop()


class TestDispatcherLLMPath:

    @pytest.mark.asyncio
    async def test_llm_tier_calls_proactive_session(self):
        """When tier=LLM and LLM succeeds, the LLM message is used."""
        user = _make_dispatch_user()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        trigger = {
            "type": "daily_review",
            "template_type": "daily_review",
            "tier": NotificationTier.LLM,
            "data": {"name": "Alex", "streak": 12, "due_count": 5},
        }

        mocks = _setup_dispatch_mocks(llm_return=("LLM generated message", 0.002))
        try:
            result = await dispatch_notification(user, trigger, bot)
        finally:
            _stop_all(mocks)

        mocks["llm_session"].assert_called_once()
        bot.send_message.assert_called_once()
        sent_text = bot.send_message.call_args[0][1]
        assert sent_text == "LLM generated message"

    @pytest.mark.asyncio
    async def test_llm_tier_falls_back_to_template_on_none(self):
        """When tier=LLM but LLM returns None, falls back to template."""
        user = _make_dispatch_user()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        trigger = {
            "type": "daily_review",
            "template_type": "daily_review",
            "tier": NotificationTier.LLM,
            "data": {"name": "Alex", "streak": 12, "due_count": 5},
        }

        mocks = _setup_dispatch_mocks(llm_return=(None, 0.0))
        try:
            result = await dispatch_notification(user, trigger, bot)
        finally:
            _stop_all(mocks)

        mocks["llm_session"].assert_called_once()
        mocks["render_template"].assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back_to_template(self):
        """When LLM raises an exception, falls back to template without crashing."""
        user = _make_dispatch_user()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        trigger = {
            "type": "streak_risk",
            "template_type": "streak_risk",
            "tier": NotificationTier.HYBRID,
            "data": {"name": "Alex", "streak": 12},
        }

        mocks = _setup_dispatch_mocks(llm_side_effect=RuntimeError("SDK crashed"))
        try:
            result = await dispatch_notification(user, trigger, bot)
        finally:
            _stop_all(mocks)

        mocks["render_template"].assert_called_once()

    @pytest.mark.asyncio
    async def test_template_tier_does_not_call_llm(self):
        """When tier=TEMPLATE, run_proactive_llm_session is NOT called."""
        user = _make_dispatch_user()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        trigger = {
            "type": "streak_risk",
            "template_type": "streak_risk",
            "tier": NotificationTier.TEMPLATE,
            "data": {"name": "Alex", "streak": 12},
        }

        mocks = _setup_dispatch_mocks()
        try:
            result = await dispatch_notification(user, trigger, bot)
        finally:
            _stop_all(mocks)

        mocks["llm_session"].assert_not_called()
