from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adaptive_lang_study_bot.bot.routers.settings import _guard_active_session


def _make_user(telegram_id: int = 123) -> MagicMock:
    user = MagicMock()
    user.telegram_id = telegram_id
    user.native_language = "en"
    return user


def _make_callback() -> AsyncMock:
    cb = AsyncMock()
    cb.answer = AsyncMock()
    return cb


class TestGuardActiveSession:

    @pytest.mark.asyncio
    async def test_blocks_when_session_active(self):
        cb = _make_callback()
        user = _make_user()

        with patch(
            "adaptive_lang_study_bot.bot.routers.settings.session_manager"
        ) as mock_sm:
            mock_sm.has_active_session.return_value = True
            result = await _guard_active_session(cb, user)

        assert result is True
        mock_sm.has_active_session.assert_called_once_with(user.telegram_id)
        cb.answer.assert_awaited_once()
        # Must use show_alert=True for popup (not toast)
        _, kwargs = cb.answer.await_args
        assert kwargs["show_alert"] is True

    @pytest.mark.asyncio
    async def test_allows_when_no_session(self):
        cb = _make_callback()
        user = _make_user()

        with patch(
            "adaptive_lang_study_bot.bot.routers.settings.session_manager"
        ) as mock_sm:
            mock_sm.has_active_session.return_value = False
            result = await _guard_active_session(cb, user)

        assert result is False
        cb.answer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_text_uses_correct_locale_key(self):
        cb = _make_callback()
        user = _make_user()

        with (
            patch(
                "adaptive_lang_study_bot.bot.routers.settings.session_manager"
            ) as mock_sm,
            patch(
                "adaptive_lang_study_bot.bot.routers.settings.t",
                return_value="blocked msg",
            ) as mock_t,
        ):
            mock_sm.has_active_session.return_value = True
            await _guard_active_session(cb, user)

        mock_t.assert_any_call("settings.active_session", "en")
        cb.answer.assert_awaited_once_with("blocked msg", show_alert=True)

    @pytest.mark.asyncio
    async def test_uses_user_native_language(self):
        cb = _make_callback()
        user = _make_user()
        user.native_language = "ru"

        with (
            patch(
                "adaptive_lang_study_bot.bot.routers.settings.session_manager"
            ) as mock_sm,
            patch(
                "adaptive_lang_study_bot.bot.routers.settings.t",
            ) as mock_t,
        ):
            mock_sm.has_active_session.return_value = True
            await _guard_active_session(cb, user)

        mock_t.assert_any_call("settings.active_session", "ru")
