from datetime import date

from adaptive_lang_study_bot.enums import CloseReason
from adaptive_lang_study_bot.pipeline.post_session import _FORCED_CLOSE_REASONS
from adaptive_lang_study_bot.utils import compute_new_streak


class TestForcedCloseReasons:
    """Test that close reasons are correctly classified as forced or voluntary."""

    def test_turn_limit_is_forced(self):
        assert CloseReason.TURN_LIMIT in _FORCED_CLOSE_REASONS

    def test_cost_limit_is_forced(self):
        assert CloseReason.COST_LIMIT in _FORCED_CLOSE_REASONS

    def test_shutdown_is_forced(self):
        assert CloseReason.SHUTDOWN in _FORCED_CLOSE_REASONS

    def test_idle_timeout_not_forced(self):
        """Idle timeout means user stopped responding — session is considered completed."""
        assert CloseReason.IDLE_TIMEOUT not in _FORCED_CLOSE_REASONS

    def test_error_is_forced(self):
        """Error close marks session as incomplete for smooth recovery."""
        assert CloseReason.ERROR in _FORCED_CLOSE_REASONS

    def test_explicit_close_not_forced(self):
        assert CloseReason.EXPLICIT_CLOSE not in _FORCED_CLOSE_REASONS

    def test_unknown_not_forced(self):
        assert CloseReason.UNKNOWN not in _FORCED_CLOSE_REASONS


class TestComputeNewStreak:
    """Test the pure streak computation function used by post_session and UserRepo."""

    def test_first_session_ever(self):
        assert compute_new_streak(0, None, date(2026, 1, 1)) == 1

    def test_consecutive_day_increments(self):
        assert compute_new_streak(5, date(2025, 12, 31), date(2026, 1, 1)) == 6

    def test_same_day_unchanged(self):
        assert compute_new_streak(5, date(2026, 1, 1), date(2026, 1, 1)) == 5

    def test_gap_two_days_grace(self):
        """2-day gap is within grace period — streak preserved but not incremented."""
        assert compute_new_streak(10, date(2026, 1, 1), date(2026, 1, 3)) == 10

    def test_gap_three_days_resets_short_streak(self):
        """3-day gap exceeds grace period — short streak resets to 1."""
        assert compute_new_streak(10, date(2026, 1, 1), date(2026, 1, 4)) == 1

    def test_gap_many_days_decays_long_streak(self):
        """Long streak (>=30) decays instead of hard reset."""
        result = compute_new_streak(50, date(2026, 1, 1), date(2026, 2, 1))
        assert result == 35  # 50 * 0.7 = 35

    def test_gap_many_days_resets_short_streak(self):
        """Short streak (<30) with large gap resets to 1."""
        assert compute_new_streak(10, date(2026, 1, 1), date(2026, 2, 1)) == 1

    def test_streak_of_one_continues(self):
        assert compute_new_streak(1, date(2026, 1, 1), date(2026, 1, 2)) == 2
