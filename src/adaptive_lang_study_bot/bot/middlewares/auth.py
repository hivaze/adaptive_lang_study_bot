from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from loguru import logger
from sqlalchemy.exc import IntegrityError

from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.db.repositories import UserRepo
from adaptive_lang_study_bot.i18n import SUPPORTED_NATIVE_LANGUAGES as _SUPPORTED_LANGUAGES
from adaptive_lang_study_bot.utils import is_user_admin


def _detect_native_language(language_code: str | None) -> str:
    """Detect native language from Telegram's language_code.

    Telegram sends IETF language tags like "en", "ru", "pt-BR", "zh-CN".
    We extract the base language and check if it's in our supported set.
    Falls back to "en" if unknown or not provided.
    """
    if not language_code:
        return "en"
    # Extract base language from IETF tag (e.g. "pt-BR" → "pt")
    base = language_code.split("-")[0].lower()
    if base in _SUPPORTED_LANGUAGES:
        return base
    return "en"


class AuthMiddleware(BaseMiddleware):
    """Look up or create the user in DB and inject into handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Extract telegram user from event
        if isinstance(event, Message):
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery):
            tg_user = event.from_user
        else:
            return await handler(event, data)

        if tg_user is None:
            return  # Ignore events without user

        db_session = data.get("db_session")
        if db_session is None:
            return await handler(event, data)

        # Try to get existing user
        user = await UserRepo.get(db_session, tg_user.id)

        if user is None:
            # Auto-detect native language from Telegram's language_code
            native_lang = _detect_native_language(tg_user.language_code)

            # Create new user with minimal data — onboarding will fill the rest.
            # Use a savepoint so that a concurrent duplicate insert (two rapid
            # first-messages from the same user) doesn't corrupt the outer
            # transaction managed by DBSessionMiddleware.
            try:
                async with db_session.begin_nested():
                    user = await UserRepo.create(
                        db_session,
                        telegram_id=tg_user.id,
                        telegram_username=tg_user.username,
                        first_name=tg_user.first_name or "User",
                        native_language=native_lang,
                        target_language="",
                    )
            except IntegrityError:
                user = await UserRepo.get(db_session, tg_user.id)
                if user is None:
                    logger.error("Failed to create or fetch user {}", tg_user.id)
                    return
            else:
                logger.info(
                    "New user registered: {} ({}) native_lang={}",
                    tg_user.id, tg_user.first_name, native_lang,
                )
        else:
            # Update username/first_name if changed
            if (
                user.telegram_username != tg_user.username
                or user.first_name != (tg_user.first_name or "User")
            ):
                await UserRepo.update_fields(
                    db_session,
                    tg_user.id,
                    telegram_username=tg_user.username,
                    first_name=tg_user.first_name or "User",
                )
                # No explicit commit — DBSessionMiddleware commits after handler

        # Block deactivated users from interacting
        if not user.is_active and not is_user_admin(user):
            # Answer callback queries so Telegram doesn't show a 30s loading spinner
            if isinstance(event, CallbackQuery):
                await event.answer()
            return

        # Whitelist mode: block non-approved, non-admin users (only /start allowed)
        if (
            settings.whitelist_mode
            and not user.whitelist_approved
            and not is_user_admin(user)
        ):
            if isinstance(event, Message) and event.text and event.text.strip().startswith("/start"):
                data["user"] = user
                data["whitelist_blocked"] = True
                return await handler(event, data)
            if isinstance(event, CallbackQuery):
                await event.answer()
            return

        # Auto-promote approved users to premium in whitelist mode
        if settings.whitelist_mode and user.whitelist_approved and user.tier != "premium":
            await UserRepo.update_fields(db_session, user.telegram_id, tier="premium")
            user.tier = "premium"

        # Auto-promote admin users to premium tier
        if is_user_admin(user) and user.tier != "premium":
            await UserRepo.update_fields(db_session, user.telegram_id, tier="premium")
            user.tier = "premium"

        data["user"] = user
        return await handler(event, data)
