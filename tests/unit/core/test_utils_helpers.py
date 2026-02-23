"""Tests for safe_zoneinfo() and compute_next_trigger() helpers."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from adaptive_lang_study_bot.utils import compute_next_trigger, safe_zoneinfo


class TestSafeZoneinfo:

    def test_valid_timezone(self):
        result = safe_zoneinfo("UTC")
        assert result == ZoneInfo("UTC")

    def test_valid_timezone_named(self):
        result = safe_zoneinfo("Europe/Moscow")
        assert result == ZoneInfo("Europe/Moscow")

    def test_invalid_timezone_falls_back(self):
        result = safe_zoneinfo("Invalid/Zone")
        assert result == timezone.utc

    def test_none_falls_back(self):
        result = safe_zoneinfo(None)
        assert result == timezone.utc

    def test_empty_string_falls_back(self):
        result = safe_zoneinfo("")
        assert result == timezone.utc


class TestComputeNextTrigger:

    def test_daily_rrule_returns_utc(self):
        tz = ZoneInfo("UTC")
        result = compute_next_trigger("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", tz)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_daily_rrule_future(self):
        tz = ZoneInfo("UTC")
        result = compute_next_trigger("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", tz)
        assert result > datetime.now(timezone.utc)

    def test_non_utc_timezone_converts(self):
        """BYHOUR=9 in Tokyo should produce a different UTC time than BYHOUR=9 in UTC."""
        utc_result = compute_next_trigger("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", ZoneInfo("UTC"))
        tokyo_result = compute_next_trigger("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", ZoneInfo("Asia/Tokyo"))
        assert utc_result is not None
        assert tokyo_result is not None
        # Tokyo is UTC+9, so 9am Tokyo = 0am UTC (different from 9am UTC)
        assert utc_result != tokyo_result

    def test_invalid_rrule_raises(self):
        with pytest.raises((ValueError, TypeError)):
            compute_next_trigger("NOT_A_VALID_RRULE", ZoneInfo("UTC"))

    def test_exhausted_rrule_returns_none(self):
        """A COUNT=1 rule that already fired should return None."""
        # Use a rule with COUNT=1 and a start time in the past
        tz = ZoneInfo("UTC")
        result = compute_next_trigger("FREQ=DAILY;COUNT=1", tz)
        # COUNT=1 with dtstart=now produces one occurrence at now, which
        # rule.after(now) skips. So it returns None.
        assert result is None
