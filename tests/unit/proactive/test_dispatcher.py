from unittest.mock import MagicMock

from adaptive_lang_study_bot.utils import user_local_now


class TestUserLocalNow:

    def test_utc_timezone(self):
        user = MagicMock()
        user.timezone = "UTC"
        now = user_local_now(user)
        assert now.tzinfo is not None

    def test_none_timezone_defaults_to_utc(self):
        user = MagicMock()
        user.timezone = None
        now = user_local_now(user)
        assert now.tzinfo is not None

    def test_invalid_timezone_defaults_to_utc(self):
        user = MagicMock()
        user.timezone = "Invalid/Timezone"
        now = user_local_now(user)
        assert now.tzinfo is not None

    def test_valid_timezone(self):
        user = MagicMock()
        user.timezone = "Europe/Moscow"
        now = user_local_now(user)
        assert now.tzinfo is not None

    def test_empty_string_timezone(self):
        user = MagicMock()
        user.timezone = ""
        now = user_local_now(user)
        assert now.tzinfo is not None
