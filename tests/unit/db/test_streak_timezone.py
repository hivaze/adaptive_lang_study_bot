"""Tests for streak timezone consistency (discovered during code audit).

The streak update (repositories.py) and streak risk trigger (triggers.py)
must both use the user's local date, not UTC date. Otherwise a user west
of UTC could have their streak recorded on the wrong date.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from adaptive_lang_study_bot.db.repositories import _user_local_date


class TestUserLocalDate:
    """Test the _user_local_date helper used by update_streak."""

    class _FakeUser:
        def __init__(self, tz: str | None):
            self.timezone = tz

    def test_utc_user(self):
        result = _user_local_date(self._FakeUser("UTC"))
        assert isinstance(result, date)

    def test_none_timezone_defaults_utc(self):
        result = _user_local_date(self._FakeUser(None))
        assert isinstance(result, date)

    def test_invalid_timezone_defaults_utc(self):
        result = _user_local_date(self._FakeUser("Invalid/Tz"))
        utc_today = datetime.now(timezone.utc).date()
        assert result == utc_today

    def test_positive_offset_can_differ_from_utc(self):
        """A user at UTC+14 may be on a different date than UTC."""
        result = _user_local_date(self._FakeUser("Pacific/Kiritimati"))
        utc_today = datetime.now(timezone.utc).date()
        # The local date might be tomorrow relative to UTC — just ensure it's valid
        diff = (result - utc_today).days
        assert diff in (0, 1)

    def test_negative_offset_can_differ_from_utc(self):
        """A user at UTC-12 may be on a different date than UTC."""
        result = _user_local_date(self._FakeUser("Etc/GMT+12"))
        utc_today = datetime.now(timezone.utc).date()
        diff = (result - utc_today).days
        assert diff in (0, -1)

    def test_matches_user_local_now_date(self):
        """_user_local_date must agree with user_local_now().date() used in triggers."""
        from adaptive_lang_study_bot.utils import user_local_now
        from unittest.mock import MagicMock

        user = MagicMock()
        user.timezone = "Asia/Tokyo"
        local_date_repo = _user_local_date(user)
        local_date_triggers = user_local_now(user).date()
        assert local_date_repo == local_date_triggers
