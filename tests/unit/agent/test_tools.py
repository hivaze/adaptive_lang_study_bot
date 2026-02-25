from unittest.mock import AsyncMock

import pytest

from adaptive_lang_study_bot.agent.tools import (
    TOOL_NAMES,
    _MAX_SCHEDULES_PER_TYPE,
    _MAX_SCHEDULES_PER_USER,
    _SESSION_TYPE_TOOLS,
    _USER_MUTABLE_FIELDS,
    _rrule_interval_minutes,
    create_session_tools,
)
from adaptive_lang_study_bot.config import TIER_LIMITS, tuning
from adaptive_lang_study_bot.enums import SessionType, UserTier


class TestToolConstants:

    def test_all_tool_names_have_mcp_prefix(self):
        for name in TOOL_NAMES:
            assert name.startswith("mcp__langbot__"), f"{name} missing prefix"

    def test_tool_count(self):
        assert len(TOOL_NAMES) == 11

    def test_session_types_defined(self):
        expected_types = {
            SessionType.INTERACTIVE, SessionType.ONBOARDING,
            SessionType.PROACTIVE_REVIEW, SessionType.PROACTIVE_QUIZ,
            SessionType.PROACTIVE_SUMMARY, SessionType.PROACTIVE_NUDGE,
            SessionType.ASSESSMENT,
        }
        assert set(_SESSION_TYPE_TOOLS.keys()) == expected_types

    def test_interactive_has_most_tools(self):
        interactive = _SESSION_TYPE_TOOLS[SessionType.INTERACTIVE]
        for session_type, tools in _SESSION_TYPE_TOOLS.items():
            if session_type != SessionType.INTERACTIVE:
                assert len(tools) <= len(interactive)

    def test_proactive_sessions_have_send_notification(self):
        for session_type, tools in _SESSION_TYPE_TOOLS.items():
            if session_type.startswith("proactive"):
                assert "send_notification" in tools, (
                    f"{session_type} missing send_notification"
                )

    def test_interactive_cannot_send_notification(self):
        assert "send_notification" not in _SESSION_TYPE_TOOLS[SessionType.INTERACTIVE]

    def test_onboarding_has_core_tools(self):
        onboarding = _SESSION_TYPE_TOOLS[SessionType.ONBOARDING]
        assert "record_exercise_result" in onboarding
        assert "add_vocabulary" in onboarding
        assert "search_vocabulary" in onboarding
        assert "update_preference" in onboarding

    def test_mutable_fields_are_safe(self):
        dangerous_fields = {"tier", "level", "is_active", "is_admin", "telegram_id",
                            "streak_days", "weak_areas", "strong_areas",
                            "recent_scores", "onboarding_completed"}
        assert not _USER_MUTABLE_FIELDS & dangerous_fields

    def test_mutable_fields_expected(self):
        expected = {"interests", "learning_goals", "preferred_difficulty", "session_style", "topics_to_avoid", "notifications_paused"}
        assert _USER_MUTABLE_FIELDS == expected

    def test_learning_goals_is_mutable(self):
        assert "learning_goals" in _USER_MUTABLE_FIELDS

    def test_session_history_not_mutable(self):
        """session_history is system-managed, not user-mutable."""
        assert "session_history" not in _USER_MUTABLE_FIELDS


class TestCanUseTool:
    """Test the can_use_tool callback returned by create_session_tools."""

    @pytest.fixture()
    def _make_can_use_tool(self):
        """Create a can_use_tool callback for a given session type."""
        def _factory(session_type: str):
            mock_session = AsyncMock()
            _, can_use_tool = create_session_tools(
                session_factory=lambda: mock_session,
                user_id=999,
                session_id="test-session-id",
                session_type=session_type,
            )
            return can_use_tool
        return _factory

    def test_interactive_allows_exercise_recording(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("record_exercise_result") is True

    def test_interactive_blocks_send_notification(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("send_notification") is False

    def test_interactive_allows_with_mcp_prefix(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("mcp__langbot__add_vocabulary") is True

    def test_onboarding_allows_exercise_tools(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.ONBOARDING)
        assert can_use("record_exercise_result") is True
        assert can_use("add_vocabulary") is True
        assert can_use("search_vocabulary") is True
        assert can_use("get_due_vocabulary") is False

    def test_onboarding_allows_preference_update(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.ONBOARDING)
        assert can_use("update_preference") is True
        assert can_use("get_user_profile") is True
        assert can_use("manage_schedule") is True

    def test_proactive_nudge_minimal_tools(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_NUDGE)
        assert can_use("get_user_profile") is True
        assert can_use("send_notification") is True
        assert can_use("add_vocabulary") is False
        assert can_use("record_exercise_result") is False

    def test_proactive_review_allows_vocab_tools(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_REVIEW)
        assert can_use("get_due_vocabulary") is True
        assert can_use("record_vocabulary_review") is True
        assert can_use("send_notification") is True
        assert can_use("add_vocabulary") is False

    def test_interactive_allows_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("get_progress_summary") is True

    def test_proactive_summary_allows_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_SUMMARY)
        assert can_use("get_progress_summary") is True

    def test_onboarding_blocks_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.ONBOARDING)
        assert can_use("get_progress_summary") is False

    def test_proactive_nudge_blocks_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_NUDGE)
        assert can_use("get_progress_summary") is False

    def test_unknown_tool_is_blocked(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("nonexistent_tool") is False

    def test_all_session_types_return_valid_callback(self, _make_can_use_tool):
        for session_type in _SESSION_TYPE_TOOLS:
            can_use = _make_can_use_tool(session_type)
            # Should return bool, not raise
            result = can_use("get_user_profile")
            assert isinstance(result, bool)

    def test_tools_created_match_session_type(self):
        """Verify create_session_tools returns the right number of tools per type."""
        for session_type, expected_names in _SESSION_TYPE_TOOLS.items():
            mock_session = AsyncMock()
            tools, _ = create_session_tools(
                session_factory=lambda: mock_session,
                user_id=999,
                session_id="test-id",
                session_type=session_type,
            )
            # All tools are created (filtering happens in session_manager)
            # but can_use_tool should only allow the right ones
            tool_names = {t.name for t in tools}
            assert expected_names.issubset(tool_names), (
                f"{session_type}: expected tools {expected_names - tool_names} not in created tools"
            )


class TestScheduleValidation:
    """Validate schedule safety constants and RRULE interval helper."""

    def test_rrule_interval_daily(self):
        assert _rrule_interval_minutes("RRULE:FREQ=DAILY") == 1440

    def test_rrule_interval_hourly(self):
        assert _rrule_interval_minutes("RRULE:FREQ=HOURLY") == 60

    def test_rrule_interval_hourly_2(self):
        assert _rrule_interval_minutes("RRULE:FREQ=HOURLY;INTERVAL=2") == 120

    def test_rrule_interval_minutely_30(self):
        assert _rrule_interval_minutes("RRULE:FREQ=MINUTELY;INTERVAL=30") == 30

    def test_rrule_interval_secondly(self):
        assert _rrule_interval_minutes("RRULE:FREQ=SECONDLY") < 1

    def test_rrule_interval_weekly(self):
        assert _rrule_interval_minutes("RRULE:FREQ=WEEKLY") == 10080

    def test_rrule_interval_invalid_raises(self):
        with pytest.raises((ValueError, TypeError)):
            _rrule_interval_minutes("not a valid rrule")

    def test_min_interval_tuning_is_at_least_hourly(self):
        assert tuning.min_schedule_interval_minutes >= 60

    def test_max_per_type_within_total(self):
        assert 1 <= _MAX_SCHEDULES_PER_TYPE <= _MAX_SCHEDULES_PER_USER

    def test_free_tier_llm_limit_is_reasonable(self):
        free = TIER_LIMITS[UserTier.FREE]
        assert 1 <= free.max_llm_notifications_per_day <= 10

    def test_premium_tier_llm_limit_is_reasonable(self):
        premium = TIER_LIMITS[UserTier.PREMIUM]
        assert premium.max_llm_notifications_per_day >= TIER_LIMITS[UserTier.FREE].max_llm_notifications_per_day


