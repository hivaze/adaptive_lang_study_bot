import time
from datetime import timezone
from html import escape as esc

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.agent.session_manager import session_manager
from adaptive_lang_study_bot.db.models import User, Vocabulary as VocabModel
from adaptive_lang_study_bot.db.repositories import UserRepo, VocabularyRepo, VocabularyReviewLogRepo
from adaptive_lang_study_bot.fsrs_engine.scheduler import review_card
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t

router = Router()

_REVIEW_FETCH_LIMIT = 20
_REVIEW_TTL = 600  # 10 minutes — reviews auto-expire

# Track users currently in flashcard review (telegram_id → monotonic timestamp).
# In-memory only — single-process bot, no persistence needed.
_active_reviews: dict[int, float] = {}


def is_in_review(user_id: int) -> bool:
    """Check if user is currently in flashcard review mode."""
    started = _active_reviews.get(user_id)
    if started is None:
        return False
    if time.monotonic() - started > _REVIEW_TTL:
        _active_reviews.pop(user_id, None)
        return False
    return True


def _start_review(user_id: int) -> None:
    _active_reviews[user_id] = time.monotonic()


def _touch_review(user_id: int) -> None:
    """Refresh review timestamp on each interaction."""
    if user_id in _active_reviews:
        _active_reviews[user_id] = time.monotonic()


def _end_review(user_id: int) -> None:
    _active_reviews.pop(user_id, None)
_RATING_KEYS = {1: "review.btn_again", 2: "review.btn_hard", 3: "review.btn_good", 4: "review.btn_easy"}


def _get_rating_label(rating: int, lang: str) -> str:
    return t(_RATING_KEYS.get(rating, "review.btn_good"), lang)


def _format_interval(scheduled_days: float, lang: str) -> str:
    """Format a scheduled_days value as a human-readable interval.

    FSRS schedules learning cards for minutes/hours, not days.
    Show the most appropriate unit instead of always "0 days".
    """
    total_minutes = scheduled_days * 1440  # 24 * 60
    if total_minutes < 1:
        return t("review.interval_now", lang)
    if total_minutes < 60:
        return t("review.interval_minutes", lang, minutes=int(total_minutes))
    total_hours = total_minutes / 60
    if total_hours < 24:
        return t("review.interval_hours", lang, hours=int(total_hours))
    return t("review.interval_days", lang, days=round(scheduled_days))


def _format_card_front(vocab, position: int, total: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Format the front of a vocabulary card (word only, no answer)."""
    lines = [
        t("review.card_front_title", lang, position=position, total=total),
        f"<b>{esc(vocab.word)}</b>",
    ]
    if vocab.topic:
        lines.append(t("review.card_topic", lang, topic=esc(vocab.topic)))
    lines.append(t("review.card_recall_hint", lang))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=t("review.btn_show_answer", lang),
                callback_data=f"fsrs:show:{vocab.id}:{position}:{total}",
            )],
            [InlineKeyboardButton(
                text=t("review.btn_done", lang),
                callback_data="fsrs:done",
            )],
        ],
    )

    return "\n".join(lines), keyboard


def _format_card_back(vocab, position: int, total: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Format the back of a vocabulary card (answer revealed, with rating buttons)."""
    lines = [
        t("review.card_front_title", lang, position=position, total=total),
        f"<b>{esc(vocab.word)}</b>",
    ]
    if vocab.translation:
        lines.append(t("review.card_translation", lang, translation=esc(vocab.translation)))
    if vocab.context_sentence:
        lines.append(f"<i>{esc(vocab.context_sentence)}</i>")

    lines.append(t("review.card_rating_hint", lang))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("review.btn_again", lang),
                    callback_data=f"fsrs:rate:{vocab.id}:{position}:{total}:1",
                ),
                InlineKeyboardButton(
                    text=t("review.btn_hard", lang),
                    callback_data=f"fsrs:rate:{vocab.id}:{position}:{total}:2",
                ),
                InlineKeyboardButton(
                    text=t("review.btn_good", lang),
                    callback_data=f"fsrs:rate:{vocab.id}:{position}:{total}:3",
                ),
                InlineKeyboardButton(
                    text=t("review.btn_easy", lang),
                    callback_data=f"fsrs:rate:{vocab.id}:{position}:{total}:4",
                ),
            ],
        ],
    )

    return "\n".join(lines), keyboard


@router.message(Command("words"))
async def cmd_review(message: Message, user: User, db_session: AsyncSession) -> None:
    lang = user.native_language or DEFAULT_LANGUAGE
    if not user.onboarding_completed:
        await message.answer(t("review.setup_first", lang))
        return

    if session_manager.has_active_session(user.telegram_id):
        await message.answer(t("review.active_session", lang))
        return

    due_cards = await VocabularyRepo.get_due(db_session, user.telegram_id, limit=_REVIEW_FETCH_LIMIT)

    if not due_cards:
        total_vocab = await VocabularyRepo.count_for_user(db_session, user.telegram_id)
        if total_vocab == 0:
            await message.answer(t("review.no_cards_empty", lang))
        else:
            await message.answer(t("review.no_cards_due", lang, total=total_vocab))
        return

    _start_review(user.telegram_id)
    total = len(due_cards)
    text, keyboard = _format_card_front(due_cards[0], 1, total, lang)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(lambda c: c.data == "fsrs:done")
async def on_fsrs_done(callback: CallbackQuery, user: User) -> None:
    """End the review session early."""
    _end_review(user.telegram_id)
    lang = user.native_language or DEFAULT_LANGUAGE
    if callback.message is not None:
        try:
            await callback.message.edit_text(t("review.ended_early", lang))
        except TelegramBadRequest:
            logger.debug("edit_text failed for review done callback")
    await callback.answer(t("review.complete", lang))


@router.callback_query(lambda c: c.data and c.data.startswith("fsrs:show:"))
async def on_fsrs_show(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """Reveal the answer side of a flashcard."""
    _touch_review(user.telegram_id)
    lang = user.native_language or DEFAULT_LANGUAGE
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer(t("review.invalid_data", lang))
        return

    try:
        vocab_id = int(parts[2])
        position = int(parts[3])
        total = int(parts[4])
    except ValueError:
        await callback.answer(t("review.invalid_data", lang))
        return

    vocab = await VocabularyRepo.get(db_session, vocab_id)
    if not vocab or vocab.user_id != user.telegram_id:
        await callback.answer(t("review.card_not_found", lang), show_alert=True)
        return

    text, keyboard = _format_card_back(vocab, position, total, lang)
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            logger.debug("edit_text failed for review show callback")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("fsrs:rate:"))
async def on_fsrs_rate(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    _touch_review(user.telegram_id)
    lang = user.native_language or DEFAULT_LANGUAGE
    parts = callback.data.split(":")
    if len(parts) != 6:
        await callback.answer(t("review.invalid_rating_data", lang))
        return

    try:
        vocab_id = int(parts[2])
        position = int(parts[3])
        total = int(parts[4])
        rating = int(parts[5])
    except ValueError:
        await callback.answer(t("review.invalid_rating_data", lang))
        return

    if rating not in _RATING_KEYS:
        await callback.answer(t("review.invalid_rating", lang))
        return

    # Fetch and validate ownership with row lock to prevent double-processing
    # when the user rapidly clicks the same rating button twice.
    result = await db_session.execute(
        sa_select(VocabModel)
        .where(VocabModel.id == vocab_id)
        .with_for_update()
    )
    vocab = result.scalar_one_or_none()
    if not vocab or vocab.user_id != user.telegram_id:
        await callback.answer(t("review.card_not_found", lang), show_alert=True)
        return

    # Guard against stale buttons: if the card was already reviewed after this
    # review session started (fsrs_last_review moved forward), skip re-processing.
    # Normalize timezone awareness to prevent TypeError on comparison.
    if vocab.fsrs_last_review and callback.message and callback.message.date:
        msg_date = callback.message.date
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        if vocab.fsrs_last_review > msg_date:
            await callback.answer(t("review.already_reviewed", lang), show_alert=False)
            return

    # Perform FSRS review
    result = review_card(vocab, rating)

    # Update vocabulary FSRS state
    await VocabularyRepo.update_fsrs(
        db_session,
        vocab_id,
        fsrs_state=result["state"],
        fsrs_stability=result["stability"],
        fsrs_difficulty=result["difficulty"],
        fsrs_due=result["due"],
        fsrs_last_review=result["last_review"],
        fsrs_data=result["card_data"],
        last_rating=rating,
    )

    # Log the review
    await VocabularyReviewLogRepo.create(
        db_session,
        vocabulary_id=vocab_id,
        user_id=user.telegram_id,
        rating=rating,
    )

    rating_label = _get_rating_label(rating, lang)
    next_interval = _format_interval(result["scheduled_days"], lang)
    next_position = position + 1

    # Check for remaining due cards
    remaining = await VocabularyRepo.get_due(db_session, user.telegram_id, limit=20)

    if not remaining:
        _end_review(user.telegram_id)
        # Clear stale notification context (e.g. "14 cards due for review")
        # so the next session doesn't reference already-completed reviews.
        if user.last_notification_text:
            await UserRepo.update_fields(
                db_session, user.telegram_id,
                last_notification_text=None, last_notification_at=None,
            )
        # Check if more cards became due beyond the current batch
        total_due = await VocabularyRepo.count_due(db_session, user.telegram_id)
        done_text = t("review.rated_done", lang,
                       word=esc(vocab.word), rating=rating_label, interval=next_interval,
                       position=position, total=total)
        if total_due > 0:
            done_text += "\n\n" + t("review.more_due", lang, count=total_due)
            done_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t("review.btn_review_more", lang),
                    callback_data="cta:words",
                )],
            ])
        else:
            done_kb = None
        if callback.message is not None:
            try:
                await callback.message.edit_text(done_text, reply_markup=done_kb)
            except TelegramBadRequest:
                logger.debug("edit_text failed for review rated_done callback")
        await callback.answer(t("review.complete", lang))
        return

    # Show next card front (active recall)
    # Adjust total if new cards became due (e.g. "Again" re-queued a card)
    adjusted_total = max(total, next_position + len(remaining) - 1)
    text, keyboard = _format_card_front(remaining[0], next_position, adjusted_total, lang)
    result_line = t("review.rated_next", lang, word=esc(vocab.word), rating=rating_label, interval=next_interval)
    if callback.message is not None:
        try:
            await callback.message.edit_text(result_line + text, reply_markup=keyboard)
        except TelegramBadRequest:
            logger.debug("edit_text failed for review rated_next callback")
    await callback.answer(t("review.rating_callback", lang, rating=rating_label, interval=next_interval))
