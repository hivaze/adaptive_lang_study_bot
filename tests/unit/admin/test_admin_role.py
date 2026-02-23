from unittest.mock import MagicMock, patch

from adaptive_lang_study_bot.utils import is_user_admin


def _make_user(*, is_admin: bool = False, telegram_id: int = 123) -> MagicMock:
    user = MagicMock()
    user.telegram_id = telegram_id
    user.is_admin = is_admin
    return user


class TestIsUserAdmin:

    def test_db_admin_returns_true(self):
        user = _make_user(is_admin=True)
        assert is_user_admin(user) is True

    def test_non_admin_returns_false(self):
        user = _make_user(is_admin=False, telegram_id=999)
        with patch("adaptive_lang_study_bot.utils.settings") as mock_settings:
            mock_settings.admin_telegram_ids = []
            assert is_user_admin(user) is False

    def test_env_var_admin_returns_true(self):
        user = _make_user(is_admin=False, telegram_id=42)
        with patch("adaptive_lang_study_bot.utils.settings") as mock_settings:
            mock_settings.admin_telegram_ids = [42, 100]
            assert is_user_admin(user) is True

    def test_env_var_other_id_returns_false(self):
        user = _make_user(is_admin=False, telegram_id=999)
        with patch("adaptive_lang_study_bot.utils.settings") as mock_settings:
            mock_settings.admin_telegram_ids = [42, 100]
            assert is_user_admin(user) is False

    def test_db_admin_ignores_env_var(self):
        """DB admin flag takes priority — no need to check env var."""
        user = _make_user(is_admin=True, telegram_id=999)
        assert is_user_admin(user) is True
