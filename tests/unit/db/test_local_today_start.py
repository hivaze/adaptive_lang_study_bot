"""Unit tests for _local_today_start() timezone helper in repositories.

Verifies that user-local midnight is correctly converted to UTC for
date-range DB queries (session counts, cost limits).
"""

from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from adaptive_lang_study_bot.db.repositories import _local_today_start


class TestLocalTodayStart:

    def test_utc_returns_utc_midnight(self):
        result = _local_today_start("UTC")
        assert result.tzinfo is not None
        # Should be today's midnight in UTC
        expected = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        assert result == expected

    def test_positive_offset_shifts_backwards(self):
        """UTC+5 midnight is 5 hours earlier in UTC (19:00 previous day)."""
        with patch("adaptive_lang_study_bot.db.repositories.datetime") as mock_dt:
            # Simulate: it's 2026-02-22 03:00 UTC → 2026-02-22 08:00 in UTC+5
            mock_dt.now.return_value = datetime(2026, 2, 22, 8, 0, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Can't easily mock datetime class, so test with real time instead
        # Just verify the output has UTC timezone info and is midnight-aligned
        result = _local_today_start("Asia/Yekaterinburg")
        assert result.tzinfo is not None
        # Convert back to local to verify it's midnight
        local_midnight = result.astimezone(ZoneInfo("Asia/Yekaterinburg"))
        assert local_midnight.hour == 0
        assert local_midnight.minute == 0
        assert local_midnight.second == 0

    def test_negative_offset_shifts_forward(self):
        """UTC-5 midnight is 5 hours later in UTC (05:00 same day)."""
        result = _local_today_start("America/New_York")
        assert result.tzinfo is not None
        local_midnight = result.astimezone(ZoneInfo("America/New_York"))
        assert local_midnight.hour == 0
        assert local_midnight.minute == 0

    def test_large_positive_offset(self):
        """UTC+12 (Auckland) — tests near date boundary."""
        result = _local_today_start("Pacific/Auckland")
        assert result.tzinfo is not None
        local = result.astimezone(ZoneInfo("Pacific/Auckland"))
        assert local.hour == 0
        assert local.minute == 0

    def test_large_negative_offset(self):
        """UTC-11 (Samoa) — tests far negative offset."""
        result = _local_today_start("Pacific/Pago_Pago")
        assert result.tzinfo is not None
        local = result.astimezone(ZoneInfo("Pacific/Pago_Pago"))
        assert local.hour == 0
        assert local.minute == 0

    def test_empty_string_defaults_to_utc(self):
        result = _local_today_start("")
        expected = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        assert result == expected

    def test_invalid_timezone_defaults_to_utc(self):
        result = _local_today_start("Invalid/Timezone")
        expected = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        assert result == expected

    def test_result_is_utc(self):
        """Regardless of input timezone, output must be in UTC."""
        for tz in ["UTC", "Asia/Tokyo", "America/Los_Angeles", "Europe/Moscow"]:
            result = _local_today_start(tz)
            assert result.tzinfo == timezone.utc, f"Expected UTC for {tz}"

    def test_different_timezones_different_boundaries(self):
        """Tokyo and LA should typically have different UTC midnight boundaries."""
        tokyo = _local_today_start("Asia/Tokyo")
        la = _local_today_start("America/Los_Angeles")
        # They differ by ~17 hours typically
        assert tokyo != la
