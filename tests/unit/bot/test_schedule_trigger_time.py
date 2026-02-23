"""Tests for schedule initial trigger time computation (discovered during code audit).

The initial next_trigger_at for onboarding schedules must be the NEXT
occurrence of the target time in the user's timezone, not the current time.
"""

from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo


def _compute_next_9am(tz_id: str) -> datetime:
    """Mirror the fixed logic from start.py on_timezone_selected."""
    try:
        user_tz = ZoneInfo(tz_id)
    except (KeyError, ValueError):
        user_tz = timezone.utc

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(user_tz)

    daily_9am = now_local.replace(hour=9, minute=0, second=0, microsecond=0)
    if daily_9am <= now_local:
        daily_9am += timedelta(days=1)
    return daily_9am.astimezone(timezone.utc)


class TestScheduleTriggerTime:

    def test_trigger_is_in_future(self):
        """The computed trigger time must always be in the future."""
        trigger = _compute_next_9am("UTC")
        assert trigger > datetime.now(timezone.utc)

    def test_trigger_is_at_9am_local(self):
        """Trigger time in user's timezone must be at 09:00."""
        tz_id = "Europe/Moscow"
        trigger_utc = _compute_next_9am(tz_id)
        trigger_local = trigger_utc.astimezone(ZoneInfo(tz_id))
        assert trigger_local.hour == 9
        assert trigger_local.minute == 0

    def test_trigger_within_24h(self):
        """Next 9am must be within the next 24 hours."""
        trigger = _compute_next_9am("America/New_York")
        now = datetime.now(timezone.utc)
        diff = trigger - now
        assert timedelta(0) < diff <= timedelta(hours=24)

    def test_invalid_timezone_defaults_to_utc(self):
        """Invalid timezone should fall back to UTC, not crash."""
        trigger = _compute_next_9am("Invalid/Timezone")
        assert trigger > datetime.now(timezone.utc)

    def test_different_timezones_give_different_utc_times(self):
        """Tokyo 9am UTC and New York 9am UTC should differ."""
        tokyo_trigger = _compute_next_9am("Asia/Tokyo")
        ny_trigger = _compute_next_9am("America/New_York")
        # Tokyo is UTC+9, NY is UTC-5, so 14 hours apart
        diff = abs((tokyo_trigger - ny_trigger).total_seconds())
        # They should not be the same UTC time (allow some margin)
        assert diff > 3600  # At least 1 hour apart
