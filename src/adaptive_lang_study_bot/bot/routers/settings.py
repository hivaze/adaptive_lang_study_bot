from datetime import time as dt_time
from html import escape as esc
from uuid import UUID

from aiogram import Router
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
from adaptive_lang_study_bot.bot.routers.start import (
    _LEVEL_CODES,
    _LEVEL_OPTIONS,
    _POPULAR_TARGET_CODES,
    _POPULAR_TZ_IDS,
    _TARGET_LANGUAGES,
    _TIMEZONE_OPTIONS,
)
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.enums import ScheduleStatus
from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    ScheduleRepo,
    UserRepo,
    VocabularyRepo,
)
from adaptive_lang_study_bot.bot.helpers import build_filterable_keyboard, get_user_lang, localize_field_name, localize_value, safe_edit_markup, safe_edit_text
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, get_localized_language_name, t
from adaptive_lang_study_bot.logging_config import is_debug_logging, set_log_level
from adaptive_lang_study_bot.utils import is_user_admin, stamp_field, stamp_fields, user_local_now

router = Router()

# Fields that can be changed via inline keyboard callbacks
_ALLOWED_SETVAL_FIELDS = {"preferred_difficulty", "session_style"}

# Valid values for each setval field (guards against crafted callbacks)
_ALLOWED_SETVAL_VALUES: dict[str, frozenset[str]] = {
    "preferred_difficulty": frozenset({"easy", "normal", "hard"}),
    "session_style": frozenset({"casual", "structured", "intensive"}),
}

# Valid timezone and target-language codes (derived from start.py data)
_VALID_TARGET_CODES = frozenset(code for code, _ in _TARGET_LANGUAGES)
_VALID_TIMEZONE_IDS = frozenset(tz_id for tz_id, _ in _TIMEZONE_OPTIONS)

# Aliases for backward compatibility within this module
_lang = get_user_lang
_safe_edit_text = safe_edit_text
_safe_edit_markup = safe_edit_markup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_owned_schedule(
    callback: CallbackQuery,
    db_session: AsyncSession,
    user: User,
    lang: str,
) -> "Schedule | None":
    """Parse schedule UUID from callback data and verify ownership.

    Returns the schedule if valid and owned, otherwise answers the callback
    with an error and returns None.
    """
    try:
        schedule_id = UUID(callback.data.split(":", 2)[2])
    except (ValueError, IndexError):
        await callback.answer(t("settings.invalid_schedule", lang), show_alert=True)
        return None
    schedule = await ScheduleRepo.get(db_session, schedule_id)
    if not schedule or schedule.user_id != user.telegram_id:
        await callback.answer(t("settings.schedule_not_found", lang), show_alert=True)
        return None
    return schedule


async def _guard_active_session(callback: CallbackQuery, user: User) -> bool:
    """Block settings changes during active session. Returns True if blocked."""
    if session_manager.has_active_session(user.telegram_id):
        lang = _lang(user)
        await callback.answer(t("settings.active_session", lang), show_alert=True)
        return True
    return False


def _build_settings_text_and_kb(user: User) -> tuple[str, InlineKeyboardMarkup]:
    """Build settings message text and keyboard."""
    lang = _lang(user)
    rows: list[list[InlineKeyboardButton]] = [
        # Learning preferences — grouped on one row
        [
            InlineKeyboardButton(text=t("settings.btn_difficulty", lang), callback_data="set:difficulty"),
            InlineKeyboardButton(text=t("settings.btn_style", lang), callback_data="set:style"),
            InlineKeyboardButton(text=t("settings.btn_level", lang), callback_data="set:level"),
        ],
        # Notification controls
        [InlineKeyboardButton(
            text=t("settings.btn_notif_on" if not user.notifications_paused else "settings.btn_notif_paused", lang),
            callback_data="set:notif_toggle",
        )],
        [
            InlineKeyboardButton(text=t("settings.btn_notif_types", lang), callback_data="set:notif_types"),
            InlineKeyboardButton(text=t("settings.btn_quiet_hours", lang), callback_data="set:quiet_hours"),
        ],
        [
            InlineKeyboardButton(
                text=t("settings.btn_max_notif", lang, count=user.max_notifications_per_day),
                callback_data="set:max_notif",
            ),
            InlineKeyboardButton(text=t("settings.btn_schedules", lang), callback_data="set:schedules"),
        ],
        # Account settings
        [
            InlineKeyboardButton(text=t("settings.btn_timezone", lang), callback_data="set:timezone"),
            InlineKeyboardButton(text=t("settings.btn_change_lang", lang), callback_data="set:change_lang"),
        ],
    ]

    # Admin-only: debug logging toggle
    if is_user_admin(user):
        debug_on = is_debug_logging()
        btn_key = "settings.btn_debug_log_on" if debug_on else "settings.btn_debug_log_off"
        rows.append([InlineKeyboardButton(
            text=t(btn_key, lang), callback_data="set:debug_log",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    native_name = get_localized_language_name(user.native_language, lang)
    target_name = get_localized_language_name(user.target_language, lang)
    lang_display = f"{esc(native_name)} → {esc(target_name)}"
    if user.native_language == user.target_language:
        lang_display += t("settings.strengthening", lang)

    notif_status = t("settings.notif_active" if not user.notifications_paused else "settings.notif_paused", lang)

    lines = [
        t("settings.title", lang),
        t("settings.language_label", lang, lang_display=lang_display),
        t("settings.difficulty_label", lang, difficulty=esc(localize_value(user.preferred_difficulty, lang))),
        t("settings.style_label", lang, style=esc(localize_value(user.session_style, lang))),
        t("settings.level_label", lang, level=esc(user.level)),
        t("settings.timezone_label", lang, timezone=esc(user.timezone)),
        t("settings.notifications_label", lang, status=notif_status),
        t("settings.max_notif_label", lang, count=user.max_notifications_per_day),
    ]

    if user.quiet_hours_start and user.quiet_hours_end:
        lines.append(
            t("settings.quiet_hours_label", lang,
              value=f"{user.quiet_hours_start.strftime('%H:%M')} – "
                    f"{user.quiet_hours_end.strftime('%H:%M')}"),
        )
    else:
        lines.append(t("settings.quiet_hours_label", lang, value=t("settings.quiet_hours_not_set", lang)))

    return "\n".join(lines), keyboard


def _build_settings_tz_kb(*, show_all: bool = False, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Build timezone keyboard for settings (stz: prefix)."""
    return build_filterable_keyboard(
        _TIMEZONE_OPTIONS,
        popular=_POPULAR_TZ_IDS,
        show_all=show_all,
        prefix="stz",
        more_callback="moretz_s:show",
        more_label=t("settings.btn_more_timezones", lang),
        back_callback="set:back",
        back_label=t("settings.btn_back", lang),
    )


def _build_settings_lang_kb(user: User, *, show_all: bool = False) -> InlineKeyboardMarkup:
    """Build language picker for settings (setlang: prefix)."""
    lang = _lang(user)
    available = [
        (code, label) for code, label in _TARGET_LANGUAGES
        if code != user.target_language
    ]
    overrides = {
        user.native_language: t(
            "start.target_strengthen", lang,
            label=dict(_TARGET_LANGUAGES).get(user.native_language, user.native_language),
        ),
    }
    return build_filterable_keyboard(
        available,
        popular=_POPULAR_TARGET_CODES,
        show_all=show_all,
        prefix="setlang",
        more_callback="morelang_s:show",
        more_label=t("settings.btn_more_languages", lang),
        back_callback="set:back",
        back_label=t("settings.btn_back", lang),
        text_override=overrides,
    )


async def _render_schedules_view(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """Render schedules view with per-schedule action buttons."""
    lang = _lang(user)
    schedules = await ScheduleRepo.get_for_user(db_session, user.telegram_id, active_only=False)

    if not schedules:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
            ],
        )
        await _safe_edit_text(
            callback,
            t("settings.no_schedules", lang),
            reply_markup=keyboard,
            lang=lang,
        )
        return

    # Convert schedule times to user's local timezone
    local_now = user_local_now(user)
    user_tz = local_now.tzinfo

    lines = [t("settings.schedules_title", lang)]
    rows: list[list[InlineKeyboardButton]] = []

    for s in schedules:
        if s.next_trigger_at:
            local_time = s.next_trigger_at.astimezone(user_tz)
            next_at = local_time.strftime("%Y-%m-%d %H:%M")
        else:
            next_at = "N/A"
        status_icon = t("settings.schedule_paused_suffix", lang) if s.status == ScheduleStatus.PAUSED else ""
        lines.append(f"  • {esc(s.description)}{status_icon} — {t('settings.schedule_next', lang, time=next_at)}")

        sid = str(s.id)
        short_desc = s.description[:20] + ("\u2026" if len(s.description) > 20 else "")
        if s.status == ScheduleStatus.ACTIVE:
            toggle_btn = InlineKeyboardButton(
                text=t("settings.btn_pause", lang, desc=short_desc),
                callback_data=f"sched:pause:{sid}",
            )
        else:
            toggle_btn = InlineKeyboardButton(
                text=t("settings.btn_resume", lang, desc=short_desc),
                callback_data=f"sched:resume:{sid}",
            )
        delete_btn = InlineKeyboardButton(text=t("settings.btn_delete", lang), callback_data=f"sched:del:{sid}")
        rows.append([toggle_btn, delete_btn])

    rows.append([InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    await _safe_edit_text(callback, "\n".join(lines), reply_markup=keyboard, lang=lang)


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

@router.message(Command("settings"))
async def cmd_settings(message: Message, user: User) -> None:
    if not user.onboarding_completed:
        await message.answer(t("settings.setup_first", _lang(user)))
        return

    text, keyboard = _build_settings_text_and_kb(user)
    await message.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Back to settings menu
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:back")
async def on_back_to_settings(
    callback: CallbackQuery, user: User,
) -> None:
    """Navigate back to the main settings menu."""
    text, keyboard = _build_settings_text_and_kb(user)
    await _safe_edit_text(callback, text, reply_markup=keyboard, lang=_lang(user))
    await callback.answer()


# ---------------------------------------------------------------------------
# Difficulty & Style pickers
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:difficulty")
async def on_difficulty(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("settings.btn_easy", lang), callback_data="setval:preferred_difficulty:easy"),
                InlineKeyboardButton(text=t("settings.btn_normal", lang), callback_data="setval:preferred_difficulty:normal"),
                InlineKeyboardButton(text=t("settings.btn_hard", lang), callback_data="setval:preferred_difficulty:hard"),
            ],
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(callback, t("settings.current_difficulty", lang, value=esc(localize_value(user.preferred_difficulty, lang))), keyboard, lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data == "set:style")
async def on_style(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("settings.btn_casual", lang), callback_data="setval:session_style:casual"),
                InlineKeyboardButton(text=t("settings.btn_structured", lang), callback_data="setval:session_style:structured"),
                InlineKeyboardButton(text=t("settings.btn_intensive", lang), callback_data="setval:session_style:intensive"),
            ],
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(callback, t("settings.current_style", lang, value=esc(localize_value(user.session_style, lang))), keyboard, lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setval:"))
async def on_set_value(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(t("settings.invalid_setting", lang))
        return

    _, field, value = parts

    if field not in _ALLOWED_SETVAL_FIELDS:
        await callback.answer(t("settings.not_allowed", lang))
        return

    allowed_values = _ALLOWED_SETVAL_VALUES.get(field)
    if allowed_values and value not in allowed_values:
        await callback.answer(t("settings.not_allowed", lang))
        return

    date_str = user_local_now(user).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, field, value, date_str)
    await UserRepo.update_fields(db_session, user.telegram_id, field_timestamps=ts, **{field: value})

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    text = t("settings.updated_field", lang, field=esc(localize_field_name(field, lang)), value=esc(localize_value(value, lang)))
    await _safe_edit_text(callback, text, reply_markup=keyboard, lang=lang)
    await callback.answer(t("settings.saved", lang))


# ---------------------------------------------------------------------------
# Level picker
# Prefix: set:level, setlvl: (selection)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:level")
async def on_level(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=t(f"start.level_{code.lower()}", lang),
            callback_data=f"setlvl:{code}",
        )]
        for code in _LEVEL_OPTIONS
    ]
    rows.append([InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    await _safe_edit_text(callback, t("settings.current_level", lang, level=esc(user.level)), keyboard, lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setlvl:"))
async def on_level_set(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    new_level = callback.data.split(":", 1)[1]

    if new_level not in _LEVEL_CODES:
        await callback.answer(t("settings.not_allowed", lang))
        return

    old_level = user.level
    if new_level == old_level:
        await callback.answer(t("settings.level_unchanged", lang))
        return

    # Update level and clear recent scores (no longer representative)
    date_str = user_local_now(user).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "level", new_level, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id, level=new_level, recent_scores=[], field_timestamps=ts,
    )

    # Queue celebration so the AI agent is aware of the level change
    milestones = dict(user.milestones or {})
    pending = list(milestones.get("pending_celebrations", []))
    pending.append(t("pipeline.level_changed", lang, old_level=old_level, new_level=new_level))
    pending = pending[-5:]
    await UserRepo.update_milestones(
        db_session, user.telegram_id, {"pending_celebrations": pending},
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    text = t("settings.level_changed", lang, old_level=esc(old_level), new_level=esc(new_level))
    await _safe_edit_text(callback, text, keyboard, lang=lang)
    await callback.answer(t("settings.level_saved", lang))


# ---------------------------------------------------------------------------
# Notification toggle
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:notif_toggle")
async def on_notif_toggle(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    new_state = not user.notifications_paused
    date_str = user_local_now(user).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "notifications_paused", new_state, date_str)
    await UserRepo.update_fields(
        db_session, user.telegram_id, notifications_paused=new_state, field_timestamps=ts,
    )

    status = t("settings.notif_paused" if new_state else "settings.notif_active", lang)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(
        callback,
        t("settings.notif_toggled", lang, status=status),
        reply_markup=keyboard,
        lang=lang,
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Debug logging toggle (admin-only)
# Prefix: set:debug_log
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:debug_log")
async def on_debug_log_toggle(
    callback: CallbackQuery, user: User,
) -> None:
    """Toggle global debug logging (admin-only)."""
    lang = _lang(user)

    if not is_user_admin(user):
        await callback.answer(t("settings.not_allowed", lang))
        return

    new_level = "INFO" if is_debug_logging() else "DEBUG"
    set_log_level(new_level)

    status_key = "settings.debug_log_enabled" if new_level == "DEBUG" else "settings.debug_log_disabled"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(callback, t(status_key, lang), reply_markup=keyboard, lang=lang)
    await callback.answer()


# ---------------------------------------------------------------------------
# Per-type notification preferences
# Prefix: set:notif_types, setntp: (toggle)
# ---------------------------------------------------------------------------

_NOTIF_PREF_CATEGORIES = [
    ("streak_reminders", "settings.notifpref_streak"),
    ("vocab_reviews", "settings.notifpref_vocab"),
    ("progress_reports", "settings.notifpref_progress"),
]


@router.callback_query(lambda c: c.data == "set:notif_types")
async def on_notif_types(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    prefs = user.notification_preferences or {}
    rows: list[list[InlineKeyboardButton]] = []
    for key, label_key in _NOTIF_PREF_CATEGORIES:
        enabled = prefs.get(key, True)
        status = "\u2713" if enabled else "\u2717"
        rows.append([InlineKeyboardButton(
            text=f"{status} {t(label_key, lang)}",
            callback_data=f"setntp:{key}",
        )])
    rows.append([InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")])
    await _safe_edit_text(callback, t("settings.notif_types_title", lang), InlineKeyboardMarkup(inline_keyboard=rows), lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setntp:"))
async def on_notif_type_toggle(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    key = callback.data.split(":", 1)[1]

    valid_keys = {k for k, _ in _NOTIF_PREF_CATEGORIES}
    if key not in valid_keys:
        await callback.answer(t("settings.not_allowed", lang))
        return

    prefs = dict(user.notification_preferences or {})
    prefs[key] = not prefs.get(key, True)
    await UserRepo.update_fields(
        db_session, user.telegram_id, notification_preferences=prefs,
    )

    # Re-render the menu with updated checkmarks
    rows: list[list[InlineKeyboardButton]] = []
    for cat_key, label_key in _NOTIF_PREF_CATEGORIES:
        enabled = prefs.get(cat_key, True)
        status = "\u2713" if enabled else "\u2717"
        rows.append([InlineKeyboardButton(
            text=f"{status} {t(label_key, lang)}",
            callback_data=f"setntp:{cat_key}",
        )])
    rows.append([InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")])
    await _safe_edit_text(callback, t("settings.notif_types_title", lang), InlineKeyboardMarkup(inline_keyboard=rows), lang=lang)
    await callback.answer(t("settings.saved", lang))


# ---------------------------------------------------------------------------
# Quiet hours
# Prefix: set:quiet_hours, setqh: (selection)
# Callback data uses HHMM format to avoid : conflicts: setqh:2200-0700
# ---------------------------------------------------------------------------

_QUIET_HOUR_PRESETS = [
    (22, 0, 7, 0, "settings.quiet_preset_night"),
    (23, 0, 8, 0, "settings.quiet_preset_late_night"),
    (21, 0, 9, 0, "settings.quiet_preset_evening"),
    (20, 0, 6, 0, "settings.quiet_preset_early_night"),
    (0, 0, 8, 0, "settings.quiet_preset_midnight"),
    (22, 0, 6, 0, "settings.quiet_preset_short_night"),
    (23, 30, 7, 30, "settings.quiet_preset_half_hour"),
    (19, 0, 6, 0, "settings.quiet_preset_long_evening"),
]


@router.callback_query(lambda c: c.data == "set:quiet_hours")
async def on_quiet_hours(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=t(label_key, lang),
            callback_data=f"setqh:{sh:02d}{sm:02d}-{eh:02d}{em:02d}",
        )]
        for sh, sm, eh, em, label_key in _QUIET_HOUR_PRESETS
    ]
    if user.quiet_hours_start and user.quiet_hours_end:
        rows.append([InlineKeyboardButton(
            text=t("settings.btn_disable_quiet", lang), callback_data="setqh:off",
        )])
    rows.append([InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")])

    current = t("settings.quiet_hours_not_set", lang)
    if user.quiet_hours_start and user.quiet_hours_end:
        current = (
            f"{user.quiet_hours_start.strftime('%H:%M')} – "
            f"{user.quiet_hours_end.strftime('%H:%M')}"
        )

    await _safe_edit_text(callback, t("settings.quiet_hours_current", lang, value=current), InlineKeyboardMarkup(inline_keyboard=rows), lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setqh:"))
async def on_quiet_hours_set(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    payload = callback.data.split(":", 1)[1]

    if payload == "off":
        await UserRepo.update_fields(
            db_session, user.telegram_id,
            quiet_hours_start=None, quiet_hours_end=None,
        )
        msg = t("settings.quiet_hours_disabled", lang)
    else:
        # Format: "HHMM-HHMM" e.g. "2200-0700"
        parts = payload.split("-")
        if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 4:
            await callback.answer(t("settings.invalid_selection", lang))
            return
        try:
            start = dt_time(int(parts[0][:2]), int(parts[0][2:]))
            end = dt_time(int(parts[1][:2]), int(parts[1][2:]))
        except ValueError:
            await callback.answer(t("settings.invalid_selection", lang))
            return

        await UserRepo.update_fields(
            db_session, user.telegram_id,
            quiet_hours_start=start, quiet_hours_end=end,
        )
        msg = t("settings.quiet_hours_set", lang,
                 start=start.strftime("%H:%M"), end=end.strftime("%H:%M"))

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(callback, msg, keyboard, lang=lang)
    await callback.answer(t("settings.saved", lang))


# ---------------------------------------------------------------------------
# Max notifications per day
# Prefix: set:max_notif, setmn: (selection)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:max_notif")
async def on_max_notif(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="1", callback_data="setmn:1"),
            InlineKeyboardButton(text="2", callback_data="setmn:2"),
            InlineKeyboardButton(text="3", callback_data="setmn:3"),
            InlineKeyboardButton(text="4", callback_data="setmn:4"),
        ],
        [
            InlineKeyboardButton(text="5", callback_data="setmn:5"),
            InlineKeyboardButton(text="8", callback_data="setmn:8"),
            InlineKeyboardButton(text="10", callback_data="setmn:10"),
        ],
        [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
    ]
    await _safe_edit_text(callback, t("settings.max_notif_current", lang, count=user.max_notifications_per_day), InlineKeyboardMarkup(inline_keyboard=rows), lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setmn:"))
async def on_max_notif_set(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    try:
        value = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer(t("settings.invalid_selection", lang))
        return

    if value < 1 or value > 10:
        await callback.answer(t("settings.invalid_selection", lang))
        return

    await UserRepo.update_fields(
        db_session, user.telegram_id, max_notifications_per_day=value,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(callback, t("settings.max_notif_set", lang, count=value), keyboard, lang=lang)
    await callback.answer(t("settings.saved", lang))


# ---------------------------------------------------------------------------
# Schedules: view + quick actions
# Prefixes: sched:pause:, sched:resume:, sched:del:, sched:cdel:
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:schedules")
async def on_view_schedules(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    await _render_schedules_view(callback, user, db_session)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sched:pause:"))
async def on_schedule_pause(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    schedule = await _get_owned_schedule(callback, db_session, user, lang)
    if not schedule:
        return
    await ScheduleRepo.update_fields(db_session, schedule.id, status=ScheduleStatus.PAUSED)
    await callback.answer(t("settings.schedule_paused_confirm", lang))
    await _render_schedules_view(callback, user, db_session)


@router.callback_query(lambda c: c.data and c.data.startswith("sched:resume:"))
async def on_schedule_resume(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    schedule = await _get_owned_schedule(callback, db_session, user, lang)
    if not schedule:
        return
    await ScheduleRepo.update_fields(db_session, schedule.id, status=ScheduleStatus.ACTIVE)
    await callback.answer(t("settings.schedule_resumed_confirm", lang))
    await _render_schedules_view(callback, user, db_session)


@router.callback_query(lambda c: c.data and c.data.startswith("sched:del:"))
async def on_schedule_delete_ask(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    lang = _lang(user)
    schedule = await _get_owned_schedule(callback, db_session, user, lang)
    if not schedule:
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("settings.btn_yes_delete", lang),
                    callback_data=f"sched:cdel:{schedule.id}",
                ),
                InlineKeyboardButton(text=t("settings.btn_cancel", lang), callback_data="set:schedules"),
            ],
        ],
    )
    await _safe_edit_text(
        callback,
        t("settings.schedule_confirm_delete", lang, desc=esc(schedule.description)),
        reply_markup=keyboard,
        lang=lang,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sched:cdel:"))
async def on_schedule_delete_confirmed(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    schedule = await _get_owned_schedule(callback, db_session, user, lang)
    if not schedule:
        return
    await ScheduleRepo.delete(db_session, schedule.id)
    await callback.answer(t("settings.schedule_deleted", lang))
    await _render_schedules_view(callback, user, db_session)


# ---------------------------------------------------------------------------
# Timezone picker (with popular subset + More...)
# Prefix: stz: (selection), moretz_s: (expand)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:timezone")
async def on_timezone(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    keyboard = _build_settings_tz_kb(lang=lang)
    await _safe_edit_text(callback, t("settings.current_timezone", lang, tz=esc(user.timezone)), keyboard, lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data == "moretz_s:show")
async def on_show_all_tz_settings(callback: CallbackQuery, user: User) -> None:
    keyboard = _build_settings_tz_kb(show_all=True, lang=_lang(user))
    await _safe_edit_markup(callback, keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("stz:"))
async def on_settings_timezone_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    tz_id = callback.data.split(":", 1)[1]

    if tz_id not in _VALID_TIMEZONE_IDS:
        await callback.answer(t("settings.not_allowed", lang))
        return

    date_str = user_local_now(user).strftime("%Y-%m-%d")
    ts = stamp_field(user.field_timestamps, "timezone", tz_id, date_str)
    await UserRepo.update_fields(db_session, user.telegram_id, timezone=tz_id, field_timestamps=ts)

    # Recalculate schedule triggers for the new timezone
    updated = await ScheduleRepo.recalculate_triggers_for_user(
        db_session, user.telegram_id, tz_id,
    )
    if updated:
        logger.info(
            "User {} timezone → {}: recalculated {} schedule triggers",
            user.telegram_id, tz_id, updated,
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    text = t("settings.timezone_changed", lang, tz=esc(tz_id))
    await callback.answer(t("settings.timezone_updated", lang))
    await _safe_edit_text(callback, text, keyboard, lang=lang)


# ---------------------------------------------------------------------------
# Target language switch flow
# Prefixes: setlang: (selection), setlangconfirm: (confirmation)
# morelang_s: (expand list)
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "set:change_lang")
async def on_change_lang(callback: CallbackQuery, user: User) -> None:
    """Show target language picker for switching."""
    lang = _lang(user)
    keyboard = _build_settings_lang_kb(user)
    current_name = get_localized_language_name(user.target_language, lang)
    await _safe_edit_text(callback, t("settings.current_target_lang", lang, target_language=esc(current_name)), keyboard, lang=lang)
    await callback.answer()


@router.callback_query(lambda c: c.data == "morelang_s:show")
async def on_show_all_lang_settings(callback: CallbackQuery, user: User) -> None:
    keyboard = _build_settings_lang_kb(user, show_all=True)
    await _safe_edit_markup(callback, keyboard)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setlang:") and not c.data.startswith("setlangconfirm:"))
async def on_lang_selected(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """Show confirmation before switching language, with concrete data counts."""
    lang = _lang(user)
    new_lang = callback.data.split(":", 1)[1]

    if new_lang not in _VALID_TARGET_CODES:
        await callback.answer(t("settings.not_allowed", lang))
        return

    new_label = next(
        (label for code, label in _TARGET_LANGUAGES if code == new_lang),
        new_lang,
    )

    # Query actual counts so the user sees exactly what will be lost.
    vocab_count = await VocabularyRepo.count_for_user(db_session, user.telegram_id)
    exercise_count = await ExerciseResultRepo.count_for_user(db_session, user.telegram_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("settings.btn_yes_switch", lang),
                    callback_data=f"setlangconfirm:{new_lang}",
                ),
                InlineKeyboardButton(
                    text=t("settings.btn_cancel", lang),
                    callback_data="set:back",
                ),
            ],
        ],
    )
    await _safe_edit_text(
        callback,
        t("settings.lang_switch_confirm", lang,
          target_language=esc(new_label),
          vocab_count=vocab_count,
          exercise_count=exercise_count,
          level=user.level,
          streak=user.streak_days),
        reply_markup=keyboard,
        lang=lang,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("setlangconfirm:"))
async def on_lang_confirmed(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """Execute the language switch: delete data, reset fields."""
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    new_lang = callback.data.split(":", 1)[1]

    if new_lang not in _VALID_TARGET_CODES:
        await callback.answer(t("settings.not_allowed", lang))
        return

    # Wrap all destructive operations in a savepoint so a partial failure
    # rolls back everything (vocabulary deletion + exercise deletion + user reset).
    async with db_session.begin_nested():
        # Delete vocabulary (cascades to vocabulary_review_log via FK)
        deleted_vocab = await VocabularyRepo.delete_for_user(db_session, user.telegram_id)

        # Delete exercise results
        deleted_exercises = await ExerciseResultRepo.delete_for_user(db_session, user.telegram_id)

        # Reset user progress fields, keep preferences and schedules
        date_str = user_local_now(user).strftime("%Y-%m-%d")
        fresh_ts = stamp_fields(None, {"target_language": new_lang, "level": "A1"}, date_str)
        await UserRepo.update_fields(
            db_session,
            user.telegram_id,
            target_language=new_lang,
            level="A1",
            weak_areas=[],
            strong_areas=[],
            recent_scores=[],
            vocabulary_count=0,
            streak_days=0,
            streak_updated_at=None,
            sessions_completed=0,
            milestones={},
            last_activity=None,
            session_history=[],
            learning_goals=[],
            field_timestamps=fresh_ts,
        )

    new_label = next(
        (label for code, label in _TARGET_LANGUAGES if code == new_lang),
        new_lang,
    )

    logger.info(
        "User {} switched target language to {} (deleted {} vocab, {} exercises)",
        user.telegram_id, new_lang, deleted_vocab, deleted_exercises,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("settings.btn_back", lang), callback_data="set:back")],
        ],
    )
    await _safe_edit_text(
        callback,
        t("settings.lang_switched", lang, target_language=esc(new_label)),
        reply_markup=keyboard,
        lang=lang,
    )
    await callback.answer(t("settings.lang_switch_confirmed", lang))


@router.callback_query(lambda c: c.data == "set:cancel")
async def on_cancel(callback: CallbackQuery, user: User) -> None:
    lang = _lang(user)
    await _safe_edit_text(callback, t("settings.cancelled", lang), lang=lang)
    await callback.answer()


# ---------------------------------------------------------------------------
# Account deletion: /deleteme
# Prefix: deletemeconfirm (confirmation)
# ---------------------------------------------------------------------------


@router.message(Command("deleteme"))
async def cmd_deleteme(message: Message, user: User) -> None:
    """Show confirmation before deleting all user data."""
    lang = _lang(user)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("deleteme.btn_confirm", lang),
                    callback_data="deletemeconfirm",
                ),
                InlineKeyboardButton(
                    text=t("deleteme.btn_cancel", lang),
                    callback_data="set:back",
                ),
            ],
        ],
    )
    await message.answer(t("deleteme.confirm", lang), reply_markup=keyboard)


@router.callback_query(lambda c: c.data == "deletemeconfirm")
async def on_deleteme_confirmed(
    callback: CallbackQuery, user: User, db_session: AsyncSession,
) -> None:
    """Delete all user data (CASCADE) and confirm."""
    if await _guard_active_session(callback, user):
        return
    lang = _lang(user)
    user_id = user.telegram_id

    # Delete user — all related tables cascade-delete via FK
    deleted = await UserRepo.delete(db_session, user_id)

    if deleted:
        logger.info("User {} deleted all their data", user_id)
        await _safe_edit_text(callback, t("deleteme.done", lang), lang=lang)
    else:
        await _safe_edit_text(callback, t("deleteme.error", lang), lang=lang)

    await callback.answer()
