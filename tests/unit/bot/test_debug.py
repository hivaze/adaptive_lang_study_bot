from adaptive_lang_study_bot.bot.routers.debug import (
    _debug_enabled,
    format_debug_info,
    is_debug_enabled,
)


class TestDebugState:

    def setup_method(self):
        _debug_enabled.clear()

    def test_debug_disabled_by_default(self):
        assert is_debug_enabled(123) is False

    def test_debug_enable(self):
        _debug_enabled.add(123)
        assert is_debug_enabled(123) is True

    def test_debug_disable(self):
        _debug_enabled.add(123)
        _debug_enabled.discard(123)
        assert is_debug_enabled(123) is False

    def test_debug_per_user(self):
        _debug_enabled.add(123)
        assert is_debug_enabled(123) is True
        assert is_debug_enabled(456) is False


class TestFormatDebugInfo:

    def test_format_contains_key_fields(self):
        debug = {
            "tools_called": ["add_vocabulary", "get_user_profile"],
            "tools_count": 2,
            "message_cost": 0.001234,
            "accumulated_cost": 0.005678,
            "turn_count": 3,
            "turns_remaining": 12,
            "tier": "free",
            "model": "claude-haiku-4-5",
            "session_duration_s": 45.2,
            "active_sessions_global": 5,
        }
        result = format_debug_info(debug)
        assert "<pre>" in result
        assert "</pre>" in result
        assert "add_vocabulary" in result
        assert "get_user_profile" in result
        assert "$0.001234" in result
        assert "claude-haiku-4-5" in result
        assert "12" in result

    def test_format_no_tools(self):
        debug = {
            "tools_called": [],
            "tools_count": 0,
            "message_cost": 0.0,
            "accumulated_cost": 0.0,
            "turn_count": 1,
            "turns_remaining": 14,
            "tier": "free",
            "model": "claude-haiku-4-5",
            "session_duration_s": 2.0,
            "active_sessions_global": 1,
        }
        result = format_debug_info(debug)
        assert "none" in result

