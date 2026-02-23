"""Tests for the logging_config module."""

from adaptive_lang_study_bot.logging_config import (
    configure_logging,
    get_current_level,
    is_debug_logging,
    set_log_level,
)


class TestConfigureLogging:

    def test_configure_sets_debug(self):
        configure_logging("DEBUG")
        assert get_current_level() == "DEBUG"
        assert is_debug_logging() is True

    def test_configure_sets_info(self):
        configure_logging("INFO")
        assert get_current_level() == "INFO"
        assert is_debug_logging() is False

    def test_configure_default_is_info(self):
        configure_logging()
        assert get_current_level() == "INFO"


class TestSetLogLevel:

    def setup_method(self):
        configure_logging("INFO")

    def test_toggle_to_debug(self):
        set_log_level("DEBUG")
        assert get_current_level() == "DEBUG"
        assert is_debug_logging() is True

    def test_toggle_back_to_info(self):
        set_log_level("DEBUG")
        set_log_level("INFO")
        assert get_current_level() == "INFO"
        assert is_debug_logging() is False

    def test_set_same_level_is_idempotent(self):
        set_log_level("INFO")
        assert get_current_level() == "INFO"
        assert is_debug_logging() is False

    def test_multiple_toggles(self):
        for _ in range(5):
            set_log_level("DEBUG")
            assert is_debug_logging() is True
            set_log_level("INFO")
            assert is_debug_logging() is False
