"""Shared bot helpers used across routers (start, settings, etc.)."""

import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t

# Maps raw DB enum values to their i18n keys for display.
_DISPLAY_VALUE_KEYS: dict[str, str] = {
    # preferred_difficulty
    "easy": "settings.btn_easy",
    "normal": "settings.btn_normal",
    "hard": "settings.btn_hard",
    # session_style
    "casual": "settings.btn_casual",
    "structured": "settings.btn_structured",
    "intensive": "settings.btn_intensive",
    # user tier
    "free": "stats.tier_free",
    "premium": "stats.tier_premium",
}

# Maps DB field names to their i18n keys for display.
_DISPLAY_FIELD_KEYS: dict[str, str] = {
    "preferred_difficulty": "settings.field_preferred_difficulty",
    "session_style": "settings.field_session_style",
}


def localize_value(value: str, lang: str) -> str:
    """Translate a raw enum value (e.g. "normal") to the user's language."""
    key = _DISPLAY_VALUE_KEYS.get(value)
    if key:
        return t(key, lang)
    return value


def localize_field_name(field: str, lang: str) -> str:
    """Translate a DB field name (e.g. "preferred_difficulty") to the user's language."""
    key = _DISPLAY_FIELD_KEYS.get(field)
    if key:
        return t(key, lang)
    return field.replace("_", " ")


# Regex to split agent output on a === line (with optional horizontal whitespace).
# Uses [ \t]* instead of \s* so newlines are not consumed greedily.
_SECTION_SPLIT_RE = re.compile(r"\n[ \t]*===[ \t]*\n")


def split_agent_sections(text: str) -> list[str]:
    """Split agent output on ``===`` line delimiters into separate message sections.

    The agent is instructed to place ``===`` on its own line between logically
    distinct sections (greeting, exercise, feedback, etc.).  Each section is
    sent as a separate Telegram message.
    """
    parts = _SECTION_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def get_user_lang(user: User) -> str:
    """Resolve user's UI language with fallback."""
    return user.native_language or DEFAULT_LANGUAGE


async def safe_edit_text(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    lang: str = DEFAULT_LANGUAGE,
) -> None:
    """Edit callback message, silently handling stale/deleted messages."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.debug("edit_text failed for callback ({}): {}", callback.data, e)
        try:
            await callback.answer(t("settings.msg_outdated", lang), show_alert=True)
        except Exception:
            pass


def build_filterable_keyboard(
    items: list[tuple[str, str]],
    *,
    popular: frozenset[str] | None = None,
    show_all: bool = False,
    prefix: str,
    more_callback: str | None = None,
    more_label: str = "",
    back_callback: str | None = None,
    back_label: str = "",
    text_override: dict[str, str] | None = None,
) -> InlineKeyboardMarkup:
    """Build a filterable inline keyboard from (code, label) items.

    Args:
        items: Full list of (code, label) pairs.
        popular: If provided and *show_all* is False, only show items in this set.
        show_all: Show all items regardless of *popular*.
        prefix: Callback data prefix (e.g. ``"tz"`` produces ``"tz:UTC"``).
        more_callback: If set, add a "show more" button when *show_all* is False.
        more_label: Text for the "show more" button.
        back_callback: If set, append a "back" button row.
        back_label: Text for the "back" button.
        text_override: Map of code -> display text to override for specific items.
    """
    visible = items if show_all or popular is None else [
        (c, l) for c, l in items if c in popular
    ]
    overrides = text_override or {}
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=overrides.get(code, label),
            callback_data=f"{prefix}:{code}",
        )]
        for code, label in visible
    ]
    if not show_all and more_callback and more_label:
        rows.append([InlineKeyboardButton(text=more_label, callback_data=more_callback)])
    if back_callback and back_label:
        rows.append([InlineKeyboardButton(text=back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_markup(
    callback: CallbackQuery,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit callback reply markup, silently handling stale/deleted messages."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest:
        logger.debug("edit_reply_markup failed for callback ({})", callback.data)
