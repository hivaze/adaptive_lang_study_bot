from unittest.mock import AsyncMock

import pytest

from adaptive_lang_study_bot.agent.tools import (
    TOOL_NAMES,
    _SESSION_TYPE_TOOLS,
    _USER_MUTABLE_FIELDS,
    _parse_list_field,
    _rrule_interval_minutes,
    create_session_tools,
)
from adaptive_lang_study_bot.config import TIER_LIMITS, tuning
from adaptive_lang_study_bot.enums import SessionType, UserTier


class TestToolConstants:

    def test_all_tool_names_have_mcp_prefix(self):
        for name in TOOL_NAMES:
            assert name.startswith("mcp__langbot__"), f"{name} missing prefix"

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

    def test_proactive_sessions_have_no_tools(self):
        """Proactive sessions are tool-less — data is pre-fetched into the prompt."""
        for session_type, tools in _SESSION_TYPE_TOOLS.items():
            if session_type.startswith("proactive"):
                assert tools == set(), (
                    f"{session_type} should have empty tool set, got {tools}"
                )

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
        expected = {"interests", "learning_goals", "preferred_difficulty", "session_style", "topics_to_avoid", "notifications_paused", "additional_notes"}
        assert _USER_MUTABLE_FIELDS == expected

    def test_manage_learning_plan_in_tool_names(self):
        assert "mcp__langbot__manage_learning_plan" in TOOL_NAMES

    def test_interactive_has_manage_learning_plan(self):
        assert "manage_learning_plan" in _SESSION_TYPE_TOOLS[SessionType.INTERACTIVE]

    def test_proactive_summary_lacks_manage_learning_plan(self):
        assert "manage_learning_plan" not in _SESSION_TYPE_TOOLS[SessionType.PROACTIVE_SUMMARY]

    def test_onboarding_lacks_manage_learning_plan(self):
        assert "manage_learning_plan" not in _SESSION_TYPE_TOOLS[SessionType.ONBOARDING]

    def test_proactive_nudge_lacks_manage_learning_plan(self):
        assert "manage_learning_plan" not in _SESSION_TYPE_TOOLS[SessionType.PROACTIVE_NUDGE]

    def test_proactive_quiz_lacks_manage_learning_plan(self):
        assert "manage_learning_plan" not in _SESSION_TYPE_TOOLS[SessionType.PROACTIVE_QUIZ]

    def test_proactive_review_lacks_manage_learning_plan(self):
        assert "manage_learning_plan" not in _SESSION_TYPE_TOOLS[SessionType.PROACTIVE_REVIEW]


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

    def test_proactive_nudge_blocks_all_tools(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_NUDGE)
        assert can_use("get_user_profile") is False
        assert can_use("add_vocabulary") is False
        assert can_use("record_exercise_result") is False

    def test_proactive_review_blocks_all_tools(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_REVIEW)
        assert can_use("get_due_vocabulary") is False
        assert can_use("add_vocabulary") is False

    def test_interactive_allows_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("get_progress_summary") is True

    def test_proactive_summary_blocks_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_SUMMARY)
        assert can_use("get_progress_summary") is False

    def test_onboarding_blocks_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.ONBOARDING)
        assert can_use("get_progress_summary") is False

    def test_proactive_nudge_blocks_progress_summary(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_NUDGE)
        assert can_use("get_progress_summary") is False

    def test_interactive_allows_manage_learning_plan(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("manage_learning_plan") is True

    def test_proactive_summary_blocks_manage_learning_plan(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_SUMMARY)
        assert can_use("manage_learning_plan") is False

    def test_onboarding_blocks_manage_learning_plan(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.ONBOARDING)
        assert can_use("manage_learning_plan") is False

    def test_proactive_nudge_blocks_manage_learning_plan(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.PROACTIVE_NUDGE)
        assert can_use("manage_learning_plan") is False

    def test_unknown_tool_is_blocked(self, _make_can_use_tool):
        can_use = _make_can_use_tool(SessionType.INTERACTIVE)
        assert can_use("nonexistent_tool") is False

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
        assert 1 <= tuning.max_schedules_per_type <= tuning.max_schedules_per_user

    def test_free_tier_llm_limit_is_reasonable(self):
        free = TIER_LIMITS[UserTier.FREE]
        assert 1 <= free.max_llm_notifications_per_day <= 10

    def test_premium_tier_llm_limit_is_reasonable(self):
        premium = TIER_LIMITS[UserTier.PREMIUM]
        assert premium.max_llm_notifications_per_day >= TIER_LIMITS[UserTier.FREE].max_llm_notifications_per_day


class TestParseListField:
    """Test _parse_list_field handles various agent output formats."""

    def test_json_array(self):
        result = _parse_list_field('["a", "b", "c"]', max_items=5, max_len=100)
        assert result == ["a", "b", "c"]

    def test_semicolon_delimited(self):
        result = _parse_list_field("Goal 1; Goal 2; Goal 3", max_items=5, max_len=200)
        assert result == ["Goal 1", "Goal 2", "Goal 3"]

    def test_comma_delimited(self):
        result = _parse_list_field("cooking, travel, music", max_items=8, max_len=100)
        assert result == ["cooking", "travel", "music"]

    def test_semicolon_preferred_over_comma(self):
        """When both ; and , are present, split on ; so commas inside items are preserved."""
        result = _parse_list_field("Goal 1, part A; Goal 2", max_items=5, max_len=200)
        assert result == ["Goal 1, part A", "Goal 2"]

    def test_plain_string_no_delimiter(self):
        result = _parse_list_field("single goal", max_items=5, max_len=200)
        assert result == ["single goal"]

    def test_max_items_enforced(self):
        result = _parse_list_field("a; b; c; d; e; f", max_items=3, max_len=100)
        assert len(result) == 3
        assert result == ["a", "b", "c"]

    def test_max_len_enforced(self):
        result = _parse_list_field("a" * 300, max_items=5, max_len=100)
        assert len(result[0]) == 100

    def test_empty_items_filtered(self):
        result = _parse_list_field("a;; b; ; c", max_items=5, max_len=100)
        assert result == ["a", "b", "c"]

    def test_non_string_value(self):
        result = _parse_list_field(42, max_items=5, max_len=100)
        assert result == ["42"]

    def test_list_value(self):
        result = _parse_list_field(["x", "y"], max_items=5, max_len=100)
        assert result == ["x", "y"]

    def test_whitespace_stripped(self):
        result = _parse_list_field("  a ;  b  ; c  ", max_items=5, max_len=100)
        assert result == ["a", "b", "c"]


