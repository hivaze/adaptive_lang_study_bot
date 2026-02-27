"""Tests for safe_zoneinfo(), compute_next_trigger(), and field timestamp helpers."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from adaptive_lang_study_bot.utils import (
    _item_key,
    compute_next_trigger,
    get_item_date,
    safe_zoneinfo,
    stamp_field,
    stamp_fields,
)


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


class TestItemKey:

    def test_deterministic(self):
        assert _item_key("cooking") == _item_key("cooking")

    def test_eight_chars(self):
        assert len(_item_key("cooking")) == 8

    def test_hex_chars(self):
        key = _item_key("cooking")
        assert all(c in "0123456789abcdef" for c in key)

    def test_strips_whitespace(self):
        assert _item_key("  cooking  ") == _item_key("cooking")

    def test_different_items_differ(self):
        assert _item_key("cooking") != _item_key("travel")


class TestStampField:

    def test_scalar_field(self):
        result = stamp_field(None, "level", "A2", "2026-02-20")
        assert result == {"level": "2026-02-20"}

    def test_scalar_overwrites(self):
        ts = {"level": "2026-01-01"}
        result = stamp_field(ts, "level", "B1", "2026-02-20")
        assert result["level"] == "2026-02-20"

    def test_does_not_mutate_input(self):
        ts = {"level": "2026-01-01"}
        result = stamp_field(ts, "level", "B1", "2026-02-20")
        assert ts["level"] == "2026-01-01"
        assert result is not ts

    def test_list_field_new_items(self):
        result = stamp_field(None, "interests", ["cooking", "travel"], "2026-02-20")
        assert "interests" in result
        cook_key = _item_key("cooking")
        travel_key = _item_key("travel")
        assert result["interests"][cook_key] == "2026-02-20"
        assert result["interests"][travel_key] == "2026-02-20"

    def test_list_field_preserves_existing(self):
        cook_key = _item_key("cooking")
        ts = {"interests": {cook_key: "2026-01-01"}}
        result = stamp_field(ts, "interests", ["cooking", "travel"], "2026-02-20")
        # cooking's date should NOT change (preserves original)
        assert result["interests"][cook_key] == "2026-01-01"
        # travel should get new date
        travel_key = _item_key("travel")
        assert result["interests"][travel_key] == "2026-02-20"

    def test_none_input_starts_fresh(self):
        result = stamp_field(None, "level", "A1", "2026-02-20")
        assert result == {"level": "2026-02-20"}


class TestStampFields:

    def test_batch_stamps(self):
        result = stamp_fields(None, {"level": "A2", "timezone": "UTC"}, "2026-02-20")
        assert result["level"] == "2026-02-20"
        assert result["timezone"] == "2026-02-20"

    def test_batch_preserves_existing(self):
        ts = {"level": "2026-01-01"}
        result = stamp_fields(ts, {"timezone": "UTC"}, "2026-02-20")
        assert result["level"] == "2026-01-01"
        assert result["timezone"] == "2026-02-20"


class TestGetItemDate:

    def test_found(self):
        cook_key = _item_key("cooking")
        ts = {"interests": {cook_key: "2026-01-15"}}
        assert get_item_date(ts, "interests", "cooking") == "2026-01-15"

    def test_not_found(self):
        ts = {"interests": {}}
        assert get_item_date(ts, "interests", "cooking") is None

    def test_none_ts(self):
        assert get_item_date(None, "interests", "cooking") is None

    def test_wrong_field(self):
        cook_key = _item_key("cooking")
        ts = {"interests": {cook_key: "2026-01-15"}}
        assert get_item_date(ts, "learning_goals", "cooking") is None

    def test_scalar_field_returns_none(self):
        """get_item_date is only for array fields; scalar fields have plain date strings."""
        ts = {"level": "2026-01-15"}
        assert get_item_date(ts, "level", "A2") is None
