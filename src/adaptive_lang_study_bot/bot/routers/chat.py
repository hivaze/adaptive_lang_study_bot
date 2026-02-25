import asyncio
import re

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.agent.session_manager import (
    _collect_session_data,
    _generate_and_send_summary,
    _log_task_exception,
    session_manager,
)
from adaptive_lang_study_bot.bot.helpers import split_agent_sections
from adaptive_lang_study_bot.bot.routers.debug import format_debug_info, is_debug_enabled
from adaptive_lang_study_bot.bot.routers.review import is_in_review
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.enums import CloseReason
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t

# Telegram enforces a 4096-character limit on message text.
TELEGRAM_MSG_MAX_LEN = 4096
# Reserve bytes for closing HTML tags when splitting long messages.
_HTML_TAG_RESERVE = 120

router = Router()


@router.message(Command("end"))
async def cmd_end(message: Message, user: User) -> None:
    """End the current session explicitly."""
    lang = user.native_language or DEFAULT_LANGUAGE
    if not user.onboarding_completed:
        await message.answer(t("chat.no_session", lang))
        return

    if not session_manager.has_active_session(user.telegram_id):
        await message.answer(t("chat.no_active_session", lang))
        return

    managed = await session_manager.close_user_session(user.telegram_id)
    if managed is not None:
        session_data = _collect_session_data(managed)
        task = asyncio.create_task(
            _generate_and_send_summary(
                message.bot, user.telegram_id, lang,
                session_data, CloseReason.EXPLICIT_CLOSE, managed,
                managed.first_name, managed.user_streak,
                managed.user_level, managed.target_language,
            )
        )
        task.add_done_callback(_log_task_exception)
    else:
        await message.answer(t("chat.session_ended", lang))


@router.message(F.text)
async def handle_text(message: Message, user: User, db_session: AsyncSession) -> None:
    """Catch-all text handler — forwards messages to the agent session."""
    lang = user.native_language or DEFAULT_LANGUAGE
    if not user.onboarding_completed:
        await message.answer(t("chat.setup_first", lang))
        return

    if not message.text:
        return

    # If user is in flashcard review mode, remind them to use buttons
    # instead of creating a new agent session.
    if is_in_review(user.telegram_id):
        await message.answer(t("review.text_during_review", lang))
        return

    # Release the middleware DB connection back to the pool before the long
    # LLM call (~5-30s).  The handler only needs the already-loaded `user`
    # object; handle_message creates its own DB sessions internally.
    await db_session.commit()

    # Refresh typing indicator every 4 seconds so it doesn't expire
    # during long session creation (~5-8s for SDK subprocess startup).
    async def _keep_typing() -> None:
        try:
            while True:
                await message.chat.do(ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())

    try:
        # Process through session manager
        response_chunks = await session_manager.handle_message(user, message.text)
    except Exception:
        logger.exception("Unhandled error in handle_message for user {}", user.telegram_id)
        await message.answer(t("chat.error", lang))
        return
    finally:
        typing_task.cancel()

    # If agent returned nothing (e.g. only tool calls), send a fallback message
    if not response_chunks or all(not c.strip() for c in response_chunks):
        await message.answer(t("session.no_response", lang))
        return

    # Send response as HTML — the agent is prompted to use Telegram HTML tags.
    # The agent may use === on its own line to split logically distinct sections
    # (greeting, exercise, feedback) into separate Telegram messages.
    # Fall back to plain text if Telegram rejects the markup.
    for chunk in response_chunks:
        sections = split_agent_sections(chunk)
        for section in sections:
            parts = _split_message(section, max_len=TELEGRAM_MSG_MAX_LEN) if len(section) > TELEGRAM_MSG_MAX_LEN else [section]
            for part in parts:
                try:
                    await message.answer(part)  # bot default is ParseMode.HTML
                except TelegramBadRequest:
                    try:
                        await message.answer(part, parse_mode=None)
                    except Exception:
                        logger.exception("Failed to send message to user {}", user.telegram_id)

    # Send debug info if enabled for this admin
    if is_debug_enabled(user.telegram_id):
        debug = session_manager.get_debug_info(user.telegram_id)
        if debug:
            try:
                debug_text = format_debug_info(debug)
                await message.answer(debug_text)
            except Exception:
                logger.warning("Failed to send debug info to user {}", user.telegram_id)

    logger.debug("Message processed for user {}", user.telegram_id)


@router.message()
async def handle_unsupported(message: Message, user: User | None = None) -> None:
    """Catch-all for non-text messages (photos, stickers, voice, etc.)."""
    lang = user.native_language if user else DEFAULT_LANGUAGE
    await message.answer(t("chat.text_only", lang))


_TAG_RE = re.compile(r"<(/?)(\w[\w-]*)([^>]*)>")


def _get_open_tags(text: str) -> list[str]:
    """Return the full opening tags that are still unclosed at the end of text."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        is_closing = match.group(1) == "/"
        tag_name = match.group(2).lower()
        if is_closing:
            # Pop the most recent matching tag
            for i in range(len(stack) - 1, -1, -1):
                open_name = _TAG_RE.match(stack[i])
                if open_name and open_name.group(2).lower() == tag_name:
                    stack.pop(i)
                    break
        else:
            stack.append(match.group(0))
    return stack


def _close_tags(open_tags: list[str]) -> str:
    """Generate closing tags in reverse order."""
    parts = []
    for tag in reversed(open_tags):
        m = _TAG_RE.match(tag)
        if m:
            parts.append(f"</{m.group(2)}>")
    return "".join(parts)


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message on paragraph boundaries, preserving HTML tag balance.

    When a split occurs inside open HTML tags, the first chunk gets closing
    tags appended and the next chunk gets the tags reopened.
    """
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break

        # Reserve space for potential closing tags (generous estimate).
        # Clamp so effective_max is always at least half of max_len.
        tag_reserve = min(_HTML_TAG_RESERVE, max_len // 2)
        effective_max = max_len - tag_reserve

        # Find the best split point (paragraph break)
        split_at = remaining.rfind("\n\n", 0, effective_max)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, effective_max)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, effective_max)
        if split_at == -1:
            split_at = effective_max

        # Guard: split_at == 0 means the delimiter is at the very start;
        # advance by at least 1 to avoid an infinite loop.
        if split_at <= 0:
            split_at = effective_max

        chunk = remaining[:split_at].rstrip()
        rest = remaining[split_at:].lstrip()

        # Balance HTML tags across the split
        open_tags = _get_open_tags(chunk) if chunk else []
        if open_tags:
            chunk += _close_tags(open_tags)
            rest = "".join(open_tags) + rest

        if chunk:
            parts.append(chunk)
        remaining = rest

    return parts or [text[:max_len]]
