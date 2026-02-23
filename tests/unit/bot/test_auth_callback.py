"""Verify deactivated user callback queries get answered (not silently dropped)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from adaptive_lang_study_bot.bot.middlewares.auth import AuthMiddleware


class TestDeactivatedUserCallbackHandling:

    @pytest.mark.asyncio
    async def test_deactivated_user_callback_gets_answered(self):
        """Deactivated user clicking inline button should get callback.answer(), not 30s spinner."""
        middleware = AuthMiddleware()
        callback = MagicMock(spec=CallbackQuery)
        callback.from_user = MagicMock()
        callback.from_user.id = 123
        callback.from_user.username = "test"
        callback.from_user.first_name = "Test"
        callback.from_user.language_code = "en"
        callback.answer = AsyncMock()

        user = MagicMock()
        user.is_active = False
        user.is_admin = False
        user.tier = "free"
        user.telegram_id = 123
        user.telegram_username = "test"
        user.first_name = "Test"

        handler = AsyncMock()
        data = {"db_session": AsyncMock()}

        with (
            patch(
                "adaptive_lang_study_bot.bot.middlewares.auth.UserRepo.get",
                new_callable=AsyncMock,
                return_value=user,
            ),
            patch(
                "adaptive_lang_study_bot.bot.middlewares.auth.is_user_admin",
                return_value=False,
            ),
        ):
            await middleware(handler, callback, data)

        # Handler must NOT be called
        handler.assert_not_called()
        # callback.answer() MUST be called so Telegram doesn't show spinner
        callback.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_deactivated_user_message_silently_dropped(self):
        """Deactivated user sending a message should be silently dropped (no answer needed)."""
        from aiogram.types import Message

        middleware = AuthMiddleware()
        message = MagicMock(spec=Message)
        message.from_user = MagicMock()
        message.from_user.id = 456
        message.from_user.username = "test2"
        message.from_user.first_name = "Test2"
        message.from_user.language_code = "en"

        user = MagicMock()
        user.is_active = False
        user.is_admin = False
        user.tier = "free"
        user.telegram_id = 456
        user.telegram_username = "test2"
        user.first_name = "Test2"

        handler = AsyncMock()
        data = {"db_session": AsyncMock()}

        with (
            patch(
                "adaptive_lang_study_bot.bot.middlewares.auth.UserRepo.get",
                new_callable=AsyncMock,
                return_value=user,
            ),
            patch(
                "adaptive_lang_study_bot.bot.middlewares.auth.is_user_admin",
                return_value=False,
            ),
        ):
            await middleware(handler, message, data)

        handler.assert_not_called()
