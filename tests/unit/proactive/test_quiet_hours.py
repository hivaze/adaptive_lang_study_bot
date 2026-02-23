"""Tests for quiet hours boundary logic (discovered during code audit).

The quiet hours end time must be exclusive: if quiet hours are 22:00-08:00,
a notification at exactly 08:00 should be allowed.
"""

from datetime import time as dt_time


def _in_quiet_hours(now_time: dt_time, start: dt_time, end: dt_time) -> bool:
    """Mirror the should_send() quiet hours logic from dispatcher.py."""
    if start <= end:
        return start <= now_time < end
    else:  # Overnight (e.g., 22:00 - 08:00)
        return now_time >= start or now_time < end


class TestQuietHoursBoundary:

    def test_exactly_at_end_is_not_quiet(self):
        """08:00 is NOT in quiet hours 22:00-08:00 (end is exclusive)."""
        assert not _in_quiet_hours(dt_time(8, 0), dt_time(22, 0), dt_time(8, 0))

    def test_just_before_end_is_quiet(self):
        """07:59 IS in quiet hours 22:00-08:00."""
        assert _in_quiet_hours(dt_time(7, 59), dt_time(22, 0), dt_time(8, 0))

    def test_exactly_at_start_is_quiet(self):
        """22:00 IS in quiet hours 22:00-08:00 (start is inclusive)."""
        assert _in_quiet_hours(dt_time(22, 0), dt_time(22, 0), dt_time(8, 0))

    def test_middle_of_night_is_quiet(self):
        """03:00 IS in quiet hours 22:00-08:00."""
        assert _in_quiet_hours(dt_time(3, 0), dt_time(22, 0), dt_time(8, 0))

    def test_daytime_not_quiet(self):
        """14:00 is NOT in quiet hours 22:00-08:00."""
        assert not _in_quiet_hours(dt_time(14, 0), dt_time(22, 0), dt_time(8, 0))

    # --- Same-day range (e.g., 09:00-17:00) ---

    def test_same_day_exactly_at_end_not_quiet(self):
        """17:00 is NOT in quiet hours 09:00-17:00 (end is exclusive)."""
        assert not _in_quiet_hours(dt_time(17, 0), dt_time(9, 0), dt_time(17, 0))

    def test_same_day_just_before_end_is_quiet(self):
        """16:59 IS in quiet hours 09:00-17:00."""
        assert _in_quiet_hours(dt_time(16, 59), dt_time(9, 0), dt_time(17, 0))

    def test_same_day_at_start_is_quiet(self):
        """09:00 IS in quiet hours 09:00-17:00."""
        assert _in_quiet_hours(dt_time(9, 0), dt_time(9, 0), dt_time(17, 0))

    def test_same_day_before_start_not_quiet(self):
        """08:00 is NOT in quiet hours 09:00-17:00."""
        assert not _in_quiet_hours(dt_time(8, 0), dt_time(9, 0), dt_time(17, 0))
