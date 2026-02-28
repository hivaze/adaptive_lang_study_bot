import asyncio

from datetime import datetime, timedelta, timezone
from html import escape as esc

from aiogram import Router
from aiogram.enums import ChatAction

from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from adaptive_lang_study_bot.agent.session_manager import session_manager
from adaptive_lang_study_bot.bot.helpers import TELEGRAM_MSG_MAX_LEN, build_filterable_keyboard, get_user_lang, markdown_to_telegram_html, safe_edit_markup, safe_edit_text, send_html_safe, split_agent_sections
from adaptive_lang_study_bot.bot.routers.chat import _split_message
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.enums import NotificationTier, ScheduleType
from adaptive_lang_study_bot.bot.routers.review import _format_card_front, _start_review, is_in_review
from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.db.repositories import AccessRequestRepo, ScheduleRepo, UserRepo, VocabularyRepo
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, get_localized_language_name, t
from adaptive_lang_study_bot.utils import safe_zoneinfo, stamp_field, stamp_fields

router = Router()

# Native language options (supported UI languages)
_NATIVE_LANGUAGES = [
    ("en", "English"),
    ("ru", "Русский"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("pt", "Português"),
    ("it", "Italiano"),
]

# Target language options (learning language)
_TARGET_LANGUAGES = [
    ("en", "English"),
    ("fr", "French / Français"),
    ("es", "Spanish / Español"),
    ("it", "Italian / Italiano"),
    ("de", "German / Deutsch"),
    ("pt", "Portuguese / Português"),
    ("ru", "Russian / Русский"),
    ("zh", "Chinese / 中文"),
    ("ja", "Japanese / 日本語"),
    ("ko", "Korean / 한국어"),
    ("ar", "Arabic / العربية"),
    ("tr", "Turkish / Türkçe"),
    ("nl", "Dutch / Nederlands"),
    ("pl", "Polish / Polski"),
    ("sv", "Swedish / Svenska"),
    ("uk", "Ukrainian / Українська"),
    ("hi", "Hindi / हिन्दी"),
]

_TIMEZONE_OPTIONS = [
    ("UTC", "UTC (GMT+0)"),
    ("Europe/Moscow", "Moscow (GMT+3)"),
    ("Europe/London", "London (GMT+0/+1)"),
    ("Europe/Paris", "Paris (GMT+1/+2)"),
    ("Europe/Berlin", "Berlin (GMT+1/+2)"),
    ("America/New_York", "New York (GMT-5/-4)"),
    ("America/Chicago", "Chicago (GMT-6/-5)"),
    ("America/Los_Angeles", "Los Angeles (GMT-8/-7)"),
    ("America/Sao_Paulo", "São Paulo (GMT-3)"),
    ("Asia/Tokyo", "Tokyo (GMT+9)"),
    ("Asia/Shanghai", "Shanghai (GMT+8)"),
    ("Asia/Seoul", "Seoul (GMT+9)"),
    ("Asia/Dubai", "Dubai (GMT+4)"),
    ("Asia/Kolkata", "Kolkata (GMT+5:30)"),
    ("Australia/Sydney", "Sydney (GMT+10/+11)"),
]

# Popular subset for target language picker (still has 17 entries)
_POPULAR_TARGET_CODES = {"en", "fr", "es", "de", "pt", "ru", "zh", "ja"}
_POPULAR_TZ_IDS = {
    "UTC", "Europe/Moscow", "Europe/London", "America/New_York",
    "Asia/Tokyo", "Asia/Shanghai",
}

# Onboarding step 4: Level self-assessment
_LEVEL_OPTIONS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Onboarding step 5: Learning goals
_GOAL_OPTIONS = ["conversation", "professional", "writing", "exams"]

# Onboarding step 6: Interests
_INTEREST_OPTIONS = [
    "food", "music", "sports", "tech", "travel", "news",
    "science", "history", "business", "art", "gaming", "health",
]


# Aliases — shared implementations live in bot.helpers
_lang = get_user_lang
_safe_edit_markup = safe_edit_markup

_TOTAL_STEPS = 7


def _step(n: int, text: str) -> str:
    """Prepend a step indicator to an onboarding message."""
    return f"<b>({n}/{_TOTAL_STEPS})</b> {text}"

# Validation sets for callback data (Fix 11)
_NATIVE_CODES = frozenset(code for code, _ in _NATIVE_LANGUAGES)
_TARGET_CODES = frozenset(code for code, _ in _TARGET_LANGUAGES)
_TIMEZONE_IDS = frozenset(tz_id for tz_id, _ in _TIMEZONE_OPTIONS)
_LEVEL_CODES = frozenset(_LEVEL_OPTIONS)


async def _safe_edit(callback: CallbackQuery, text: str, **kwargs: object) -> None:
    """Thin wrapper around safe_edit_text that accepts **kwargs for convenience."""
    await safe_edit_text(callback, text, reply_markup=kwargs.get("reply_markup"))


# ---------------------------------------------------------------------------
# Keyboard builders (reusable for back-button navigation and step resume)
# ---------------------------------------------------------------------------

def _build_native_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=label, callback_data=f"native:{code}")]
        for code, label in _NATIVE_LANGUAGES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_target_kb(
    native_code: str, *, show_all: bool = False, lang: str = DEFAULT_LANGUAGE,
    show_back: bool = True,
) -> InlineKeyboardMarkup:
    overrides = {
        native_code: t("start.target_strengthen", lang, label=dict(_TARGET_LANGUAGES).get(native_code, native_code)),
    }
    return build_filterable_keyboard(
        _TARGET_LANGUAGES,
        popular=_POPULAR_TARGET_CODES,
        show_all=show_all,
        prefix="target",
        more_callback="moretarget:show",
        more_label=t("start.btn_more_languages", lang),
        back_callback="back:native" if show_back else None,
        back_label=t("start.btn_back", lang) if show_back else None,
        text_override=overrides,
    )


def _build_timezone_kb(*, show_all: bool = False, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    return build_filterable_keyboard(
        _TIMEZONE_OPTIONS,
        popular=_POPULAR_TZ_IDS,
        show_all=show_all,
        prefix="tz",
        more_callback="moretz:show",
        more_label=t("start.btn_more_timezones", lang),
        back_callback="back:target",
        back_label=t("start.btn_back", lang),
    )


def _build_level_kb(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=t(f"start.level_{code.lower()}", lang),
            callback_data=f"level:{code}",
        )]
        for code in _LEVEL_OPTIONS
    ]
    rows.append([InlineKeyboardButton(text=t("start.btn_back", lang), callback_data="back:tz")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_goals_kb(
    selected: list[str], lang: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for code in _GOAL_OPTIONS:
        label = t(f"start.goal_{code}", lang)
        if code in selected:
            label = f"\u2713 {label}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"goal:{code}")])
    rows.append([
        InlineKeyboardButton(text=t("start.btn_done", lang), callback_data="goaldone:save"),
        InlineKeyboardButton(text=t("start.btn_skip", lang), callback_data="goaldone:skip"),
    ])
    rows.append([InlineKeyboardButton(text=t("start.btn_back", lang), callback_data="back:level")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_interests_kb(
    selected: list[str], lang: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(_INTEREST_OPTIONS), 2):
        row = []
        for code in _INTEREST_OPTIONS[i : i + 2]:
            label = t(f"start.interest_{code}", lang)
            if code in selected:
                label = f"\u2713 {label}"
            row.append(InlineKeyboardButton(text=label, callback_data=f"interest:{code}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text=t("start.btn_done", lang), callback_data="interestdone:save"),
        InlineKeyboardButton(text=t("start.btn_skip", lang), callback_data="interestdone:skip"),
    ])
    rows.append([InlineKeyboardButton(text=t("start.btn_back", lang), callback_data="back:goal")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_schedule_pref_kb(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=t("start.sched_early", lang), callback_data="schedpref:7")],
        [InlineKeyboardButton(text=t("start.sched_morning", lang), callback_data="schedpref:9")],
        [InlineKeyboardButton(text=t("start.sched_afternoon", lang), callback_data="schedpref:14")],
        [InlineKeyboardButton(text=t("start.sched_evening", lang), callback_data="schedpref:19")],
        [InlineKeyboardButton(text=t("start.sched_late", lang), callback_data="schedpref:21")],
        [InlineKeyboardButton(text=t("start.sched_none", lang), callback_data="schedpref:none")],
        [InlineKeyboardButton(text=t("start.btn_back", lang), callback_data="back:interest")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Whitelist helpers
# ---------------------------------------------------------------------------

async def _notify_admins_about_request(bot: object, user: User) -> None:
    """Notify admin(s) via Telegram about a new whitelist access request."""
    text = (
        f"<b>New access request</b>\n\n"
        f"Name: {esc(user.first_name)}\n"
        f"Username: @{esc(user.telegram_username) if user.telegram_username else 'N/A'}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n\n"
        f"Review in the admin panel."
    )
    for admin_id in settings.admin_telegram_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            logger.warning("Failed to notify admin {} about access request", admin_id)


# ---------------------------------------------------------------------------
# /start — Onboarding with step resume
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(
    message: Message, user: User, db_session: AsyncSession,
    whitelist_blocked: bool = False,
) -> None:
    lang = _lang(user)

    # Whitelist mode: non-approved user can only request access
    if whitelist_blocked:
        existing = await AccessRequestRepo.get_by_telegram_id(
            db_session, user.telegram_id, status="pending",
        )
        if existing:
            await message.answer(t("whitelist.already_requested", lang))
            return

        await AccessRequestRepo.create(
            db_session,
            telegram_id=user.telegram_id,
            telegram_username=user.telegram_username,
            first_name=user.first_name,
            language_code=user.native_language,
        )
        await _notify_admins_about_request(message.bot, user)
        await message.answer(t("whitelist.request_sent", lang))
        return

    if user.onboarding_completed:
        await message.answer(t("start.welcome_back", lang, name=esc(user.first_name)))
        return

    # Resume onboarding from where the user left off.
    milestones = user.milestones or {}
    step = milestones.get("onboarding_step")

    if step == 7:
        keyboard = _build_schedule_pref_kb(lang=lang)
        await message.answer(
            _step(7, t("start.resume_schedule", lang, name=esc(user.first_name))),
            reply_markup=keyboard,
        )
        return

    if step == 6:
        selected = milestones.get("onboarding_interests", [])
        keyboard = _build_interests_kb(selected, lang=lang)
        await message.answer(
            _step(6, t("start.resume_interests", lang, name=esc(user.first_name))),
            reply_markup=keyboard,
        )
        return

    if step == 5:
        selected_goals = milestones.get("onboarding_goals", [])
        keyboard = _build_goals_kb(selected_goals, lang=lang)
        await message.answer(
            _step(5, t("start.resume_goal", lang, name=esc(user.first_name))),
            reply_markup=keyboard,
        )
        return

    if step == 4:
        keyboard = _build_level_kb(lang=lang)
        await message.answer(
            _step(4, t("start.resume_level", lang, name=esc(user.first_name))),
            reply_markup=keyboard,
        )
        return

    # AuthMiddleware creates users with native_language=<auto> and target_language="".
    if user.target_language:
        keyboard = _build_timezone_kb(lang=lang)
        await message.answer(
            _step(3, t("start.resume_timezone", lang, name=esc(user.first_name))),
            reply_markup=keyboard,
        )
        return

    if user.native_language:
        # Distinguish fresh user (native auto-detected by AuthMiddleware, no milestones)
        # from a user who previously started onboarding and is resuming.
        has_milestones = bool(user.milestones)
        if has_milestones:
            # Resuming onboarding — user already confirmed native language
            keyboard = _build_target_kb(user.native_language, lang=lang, show_back=True)
            await message.answer(
                _step(2, t("start.resume_target", lang, name=esc(user.first_name))),
                reply_markup=keyboard,
            )
        else:
            # Fresh user — show step 1 with auto-detected language confirmation
            detected_label = dict(_NATIVE_LANGUAGES).get(user.native_language, user.native_language)
            keyboard = _build_native_kb()
            await message.answer(
                _step(1, t("start.native_detected", lang, name=esc(user.first_name), language=detected_label)),
                reply_markup=keyboard,
            )
        return

    # Fresh start — step 1: native language
    keyboard = _build_native_kb()
    await message.answer(
        _step(1, t("start.welcome_new", DEFAULT_LANGUAGE, name=esc(user.first_name))),
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Step 1 → 2: Native language selected
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("native:"))
async def on_native_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", _lang(user)), show_alert=True)
        return

    native_code = callback.data.split(":", 1)[1]
    if native_code not in _NATIVE_CODES:
        await callback.answer(t("start.invalid_selection", DEFAULT_LANGUAGE), show_alert=True)
        return

    # Clear target_language when native changes — prevents stale pairing
    # if the user goes back during onboarding and picks a different native.
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "native_language", native_code, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id,
        native_language=native_code,
        target_language="",
        field_timestamps=ts,
    )

    # Get native label for display
    native_label = next(
        (label for code, label in _NATIVE_LANGUAGES if code == native_code),
        native_code,
    )

    # Now use the newly selected native language for UI
    lang = native_code

    await callback.answer(t("start.native_selected", lang, label=native_label))

    # Step 2: Ask for target language
    keyboard = _build_target_kb(native_code, lang=lang)

    await _safe_edit(callback, _step(2, t("start.ask_target", lang)), reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Step 2 → 3: Target language selected
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("target:"))
async def on_target_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", lang), show_alert=True)
        return

    target_code = callback.data.split(":", 1)[1]
    if target_code not in _TARGET_CODES:
        await callback.answer(t("start.invalid_selection", lang), show_alert=True)
        return

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "target_language", target_code, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id,
        target_language=target_code,
        field_timestamps=ts,
    )

    target_label = next(
        (label for code, label in _TARGET_LANGUAGES if code == target_code),
        target_code,
    )

    await callback.answer(t("start.target_selected", lang, label=target_label))

    # Step 3: Ask for timezone
    keyboard = _build_timezone_kb(lang=lang)

    await _safe_edit(callback, _step(3, t("start.ask_timezone", lang)), reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Step 3 → 4: Timezone selected
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("tz:"))
async def on_timezone_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_tz", lang), show_alert=True)
        return

    tz_id = callback.data.split(":", 1)[1]
    if tz_id not in _TIMEZONE_IDS:
        await callback.answer(t("start.invalid_selection", lang), show_alert=True)
        return

    # Save timezone and advance to step 4 (level assessment)
    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 4

    # Use new timezone for the date stamp
    user_tz = safe_zoneinfo(tz_id)
    date_str = datetime.now(timezone.utc).astimezone(user_tz).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "timezone", tz_id, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id,
        timezone=tz_id,
        milestones=milestones,
        field_timestamps=ts,
    )

    await callback.answer(t("start.timezone_set", lang))

    # Step 4: Ask for level self-assessment
    target_name = get_localized_language_name(user.target_language, lang)
    keyboard = _build_level_kb(lang=lang)
    await _safe_edit(
        callback,
        _step(4, t("start.ask_level", lang, target_language=target_name)),
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Step 4 → 5: Level self-assessment selected
# Prefix: level: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("level:"))
async def on_level_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", lang), show_alert=True)
        return

    level_code = callback.data.split(":", 1)[1]
    if level_code not in _LEVEL_CODES:
        await callback.answer(t("start.invalid_selection", lang), show_alert=True)
        return

    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 5

    user_tz = safe_zoneinfo(user.timezone)
    date_str = datetime.now(timezone.utc).astimezone(user_tz).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "level", level_code, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id,
        level=level_code,
        milestones=milestones,
        field_timestamps=ts,
    )

    await callback.answer(t("start.level_set", lang))

    # Step 5: Ask for learning goal (multi-select)
    selected_goals = milestones.get("onboarding_goals", [])
    keyboard = _build_goals_kb(selected_goals, lang=lang)
    target_name = get_localized_language_name(user.target_language, lang)
    await _safe_edit(
        callback,
        _step(5, t("start.ask_goal", lang, target_language=target_name)),
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Step 5: Goal multi-select toggle
# Prefix: goal: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("goal:") and not c.data.startswith("goaldone:"))
async def on_goal_toggled(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer()
        return

    code = callback.data.split(":", 1)[1]

    milestones = dict(user.milestones or {})
    selected = list(milestones.get("onboarding_goals", []))
    if code in selected:
        selected.remove(code)
    else:
        selected.append(code)

    milestones["onboarding_goals"] = selected
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)

    # Re-render keyboard with updated checkmarks
    keyboard = _build_goals_kb(selected, lang=lang)
    await _safe_edit_markup(callback, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 5 → 6: Goals done / skip
# Prefix: goaldone: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("goaldone:"))
async def on_goals_done(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer()
        return

    action = callback.data.split(":", 1)[1]

    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 6
    updates: dict = {"milestones": milestones}

    if action == "save":
        selected = milestones.get("onboarding_goals", [])
        if selected:
            goals = [c for c in selected if c in _GOAL_OPTIONS]
            updates["learning_goals"] = goals
            user_tz = safe_zoneinfo(user.timezone)
            date_str = datetime.now(timezone.utc).astimezone(user_tz).strftime("%Y-%m-%d")
            ts = stamp_field(user.field_timestamps, "learning_goals", goals, date_str)
            updates["field_timestamps"] = ts

    # Clean up temp selection
    milestones.pop("onboarding_goals", None)

    await UserRepo.update_fields(db_session, user.telegram_id, **updates)

    toast = t("start.goal_set", lang) if action == "save" else t("start.skipped", lang)
    await callback.answer(toast)

    # Step 6: Ask for interests
    selected_interests = milestones.get("onboarding_interests", [])
    keyboard = _build_interests_kb(selected_interests, lang=lang)
    await _safe_edit(callback, _step(6, t("start.ask_interests", lang)), reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Step 6: Interest multi-select toggle
# Prefix: interest: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("interest:") and not c.data.startswith("interestdone:"))
async def on_interest_toggled(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer()
        return

    code = callback.data.split(":", 1)[1]

    milestones = dict(user.milestones or {})
    selected = list(milestones.get("onboarding_interests", []))
    if code in selected:
        selected.remove(code)
    else:
        selected.append(code)

    milestones["onboarding_interests"] = selected
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)

    # Re-render keyboard with updated checkmarks
    keyboard = _build_interests_kb(selected, lang=lang)
    await _safe_edit_markup(callback, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Step 6 → 7: Interests done / skip
# Prefix: interestdone: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("interestdone:"))
async def on_interests_done(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer()
        return

    action = callback.data.split(":", 1)[1]

    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 7

    updates: dict = {"milestones": milestones}

    if action == "save":
        selected = milestones.get("onboarding_interests", [])
        if selected:
            interests = [c for c in selected if c in _INTEREST_OPTIONS]
            updates["interests"] = interests
            user_tz = safe_zoneinfo(user.timezone)
            date_str = datetime.now(timezone.utc).astimezone(user_tz).strftime("%Y-%m-%d")
            ts = stamp_field(user.field_timestamps, "interests", interests, date_str)
            updates["field_timestamps"] = ts

    # Clean up temp selection
    milestones.pop("onboarding_interests", None)

    await UserRepo.update_fields(db_session, user.telegram_id, **updates)

    toast = t("start.interests_set", lang) if action == "save" else t("start.skipped", lang)
    await callback.answer(toast)

    # Step 7: Ask for schedule preference
    keyboard = _build_schedule_pref_kb(lang=lang)
    await _safe_edit(callback, _step(7, t("start.ask_schedule", lang)), reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Step 7 (final): Schedule preference — complete onboarding
# Prefix: schedpref: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("schedpref:"))
async def on_schedule_pref_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)

    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_tz", lang), show_alert=True)
        return

    pref = callback.data.split(":", 1)[1]

    if pref not in {"7", "9", "14", "19", "21", "none"}:
        await callback.answer()
        return

    # Clean up onboarding tracking from milestones
    milestones = dict(user.milestones or {})
    milestones.pop("onboarding_step", None)
    milestones.pop("onboarding_goals", None)
    milestones.pop("onboarding_interests", None)

    await UserRepo.update_fields(
        db_session, user.telegram_id,
        onboarding_completed=True,
        milestones=milestones,
    )

    if pref != "none":
        hour = int(pref)
        user_tz = safe_zoneinfo(user.timezone or "UTC")
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(user_tz)

        # Next occurrence of the chosen hour
        daily_local = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if daily_local <= now_local:
            daily_local += timedelta(days=1)
        daily_trigger_utc = daily_local.astimezone(timezone.utc)

        # Weekly report: Sunday at chosen_hour + 1 (wraps to Monday 0:00 if hour=23)
        report_hour = (hour + 1) % 24
        wraps_day = hour >= 23  # report lands on next day
        report_day = "MO" if wraps_day else "SU"
        # Target weekday index: Sunday=6, Monday=0
        target_weekday = 0 if wraps_day else 6
        weekly_local = now_local.replace(hour=report_hour, minute=0, second=0, microsecond=0)
        days_until_target = (target_weekday - now_local.weekday()) % 7
        if days_until_target == 0 and weekly_local <= now_local:
            days_until_target = 7
        weekly_local += timedelta(days=days_until_target)
        weekly_trigger_utc = weekly_local.astimezone(timezone.utc)

        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type=ScheduleType.DAILY_REVIEW,
            rrule=f"FREQ=DAILY;BYHOUR={hour};BYMINUTE=0",
            next_trigger_at=daily_trigger_utc,
            description=t("start.sched_desc_daily", lang, time=f"{hour}:00"),
            notification_tier=NotificationTier.TEMPLATE,
            created_by="onboarding",
        )

        await ScheduleRepo.create(
            db_session,
            user_id=user.telegram_id,
            schedule_type=ScheduleType.PROGRESS_REPORT,
            rrule=f"FREQ=WEEKLY;BYDAY={report_day};BYHOUR={report_hour};BYMINUTE=0",
            next_trigger_at=weekly_trigger_utc,
            description=t("start.sched_desc_weekly", lang, time=f"{report_hour}:00"),
            notification_tier=NotificationTier.LLM,
            created_by="onboarding",
        )

        await callback.answer(t("start.schedule_set", lang))
        await _safe_edit(
            callback,
            t("start.onboarding_complete_sched", lang, time=f"{hour}:00"),
        )
    else:
        await callback.answer()
        await _safe_edit(callback, t("start.onboarding_complete_nosched", lang))

    logger.info("User {} onboarding completed", user.telegram_id)

    # Auto-start first session so the user doesn't face a dead end.
    # Refresh user object with onboarding_completed=True before passing to session manager.
    user.onboarding_completed = True
    if callback.message:
        asyncio.create_task(
            _auto_start_first_session(callback.message, user, lang),
        )


async def _auto_start_first_session(message: Message, user: User, lang: str) -> None:
    """Start the first interactive session automatically after onboarding."""
    try:
        await message.answer(t("start.first_session_starting", lang))
        await message.chat.do(ChatAction.TYPING)

        first_prompt = t("start.first_session_prompt", lang)
        response_chunks = await session_manager.handle_message(user, first_prompt)

        if not response_chunks or all(not c.strip() for c in response_chunks):
            return

        for chunk in response_chunks:
            sections = [markdown_to_telegram_html(s) for s in split_agent_sections(chunk)]
            for section in sections:
                parts = _split_message(section, max_len=TELEGRAM_MSG_MAX_LEN) if len(section) > TELEGRAM_MSG_MAX_LEN else [section]
                for part in parts:
                    await send_html_safe(message.answer, part, user_id=user.telegram_id)
    except Exception:
        logger.exception("Failed to auto-start first session for user {}", user.telegram_id)
        try:
            cta_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t("cta.start_session", lang),
                    callback_data="cta:session",
                )],
            ])
            await message.answer(
                t("start.first_session_error", lang),
                reply_markup=cta_keyboard,
            )
        except Exception:
            logger.warning("Failed to send first-session error fallback to user {}", user.telegram_id)


# ---------------------------------------------------------------------------
# Back navigation handlers
# Prefix: back: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "back:native")
async def on_back_to_native(callback: CallbackQuery, user: User) -> None:
    """Go back to native language selection during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", lang), show_alert=True)
        return
    keyboard = _build_native_kb()
    await _safe_edit(
        callback,
        _step(1, t("start.welcome_new", DEFAULT_LANGUAGE, name=esc(user.first_name))),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:target")
async def on_back_to_target(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """Go back to target language selection during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", lang), show_alert=True)
        return
    # Clear onboarding_step and downstream selections (goals, interests)
    # so the forward flow starts fresh after changing target language.
    milestones = dict(user.milestones or {})
    changed = False
    for key in ("onboarding_step", "onboarding_goals", "onboarding_interests"):
        if key in milestones:
            milestones.pop(key)
            changed = True
    if changed:
        await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)
    keyboard = _build_target_kb(user.native_language, lang=lang)
    await _safe_edit(callback, _step(2, t("start.ask_target", lang)), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:tz")
async def on_back_to_tz(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """Go back to timezone selection during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_tz", lang), show_alert=True)
        return
    # Reset step so /start resume lands on timezone
    milestones = dict(user.milestones or {})
    milestones.pop("onboarding_step", None)
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)
    keyboard = _build_timezone_kb(lang=lang)
    await _safe_edit(callback, _step(3, t("start.ask_timezone", lang)), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:level")
async def on_back_to_level(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """Go back to level assessment during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer()
        return
    # Reset step so /start resume lands on level
    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 4
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)
    target_name = get_localized_language_name(user.target_language, lang)
    keyboard = _build_level_kb(lang=lang)
    await _safe_edit(
        callback,
        _step(4, t("start.ask_level", lang, target_language=target_name)),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:goal")
async def on_back_to_goal(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """Go back to goal selection during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer()
        return
    # Reset step so /start resume lands on goal
    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 5
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)
    selected_goals = milestones.get("onboarding_goals", [])
    keyboard = _build_goals_kb(selected_goals, lang=lang)
    target_name = get_localized_language_name(user.target_language, lang)
    await _safe_edit(
        callback,
        _step(5, t("start.ask_goal", lang, target_language=target_name)),
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:interest")
async def on_back_to_interests(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """Go back to interest selection during onboarding."""
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer()
        return
    # Reset step so /start resume lands on interests
    milestones = dict(user.milestones or {})
    milestones["onboarding_step"] = 6
    await UserRepo.update_fields(db_session, user.telegram_id, milestones=milestones)
    selected = milestones.get("onboarding_interests", [])
    keyboard = _build_interests_kb(selected, lang=lang)
    await _safe_edit(callback, _step(6, t("start.ask_interests", lang)), reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# "More" expansion handlers
# Prefixes: moretarget:, moretz: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "moretarget:show")
async def on_show_all_target(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_lang", lang), show_alert=True)
        return
    keyboard = _build_target_kb(user.native_language, show_all=True, lang=lang)
    await _safe_edit_markup(callback, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "moretz:show")
async def on_show_all_tz(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    if user.onboarding_completed:
        await callback.answer(t("start.already_onboarded_tz", lang), show_alert=True)
        return
    keyboard = _build_timezone_kb(show_all=True, lang=lang)
    await _safe_edit_markup(callback, reply_markup=keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# Proactive notification CTA buttons
# Prefix: cta: (unique to start.py)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "cta:words")
async def on_cta_words(callback: CallbackQuery, user: User, db_session: AsyncSession) -> None:
    """CTA button from notification — directly start a vocabulary review."""
    lang = _lang(user)
    if not user.onboarding_completed:
        await callback.answer(t("start.setup_first", lang), show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return

    # Block if an agent session is active (same guard as /words command)
    if session_manager.has_active_session(user.telegram_id):
        await callback.answer(t("review.active_session", lang), show_alert=True)
        return

    # Block if already in a vocabulary review
    if is_in_review(user.telegram_id):
        await callback.answer(t("review.active_review", lang), show_alert=True)
        return

    # Directly trigger the review flow instead of telling the user to type /words.
    due_cards = await VocabularyRepo.get_due(db_session, user.telegram_id, limit=20)
    if not due_cards:
        total_vocab = await VocabularyRepo.count_for_user(db_session, user.telegram_id)
        if total_vocab == 0:
            await callback.message.answer(t("review.no_cards_empty", lang))
        else:
            await callback.message.answer(t("review.no_cards_due", lang, total=total_vocab))
        await callback.answer()
        return

    _start_review(user.telegram_id)
    total = len(due_cards)
    text, keyboard = _format_card_front(due_cards[0], 1, total, lang)
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data == "cta:session")
async def on_cta_session(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """CTA button from notification — auto-start an interactive session."""
    lang = _lang(user)
    if not user.onboarding_completed:
        await callback.answer(t("start.setup_first", lang), show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return

    # Block if a vocabulary review is active — avoid overlapping sessions.
    if is_in_review(user.telegram_id):
        await callback.answer(t("review.active_review", lang), show_alert=True)
        return

    # Release middleware DB connection before the long LLM call.
    await db_session.commit()

    await callback.message.answer(t("start.cta_session_continuing", lang))
    await callback.answer()

    async def _keep_typing() -> None:
        try:
            while True:
                await callback.message.chat.do(ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())

    try:
        prompt = t("start.cta_continue_prompt", lang)
        response_chunks = await session_manager.handle_message(user, prompt)
    except Exception:
        logger.exception("CTA session start failed for user {}", user.telegram_id)
        await callback.message.answer(t("start.cta_session_prompt", lang))
        return
    finally:
        typing_task.cancel()

    if not response_chunks or all(not c.strip() for c in response_chunks):
        return

    for chunk in response_chunks:
        sections = [markdown_to_telegram_html(s) for s in split_agent_sections(chunk)]
        for section in sections:
            parts = (
                _split_message(section, max_len=TELEGRAM_MSG_MAX_LEN)
                if len(section) > TELEGRAM_MSG_MAX_LEN else [section]
            )
            for part in parts:
                await send_html_safe(callback.message.answer, part, user_id=user.telegram_id)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

@router.message(Command("help"))
async def cmd_help(message: Message, user: User) -> None:
    lang = _lang(user)

    if not user.onboarding_completed:
        await message.answer(t("help.new_user", lang))
        return

    if user.sessions_completed == 0:
        await message.answer(t("help.getting_started", lang))
        return

    if user.last_session_at:
        gap_hours = (datetime.now(timezone.utc) - user.last_session_at).total_seconds() / 3600
        if gap_hours > 72:
            await message.answer(t("help.returning", lang))
            return

    await message.answer(t("help.full", lang))
