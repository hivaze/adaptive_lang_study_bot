import hashlib
from datetime import date, datetime, timezone
from datetime import tzinfo as _tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from adaptive_lang_study_bot.config import settings, tuning
from adaptive_lang_study_bot.db.models import User


def safe_zoneinfo(tz_str: str | None) -> _tzinfo:
    """Resolve timezone string to ZoneInfo, falling back to UTC."""
    if not tz_str:
        return timezone.utc
    try:
        return ZoneInfo(tz_str)
    except (KeyError, ValueError):
        return timezone.utc


def compute_next_trigger(rrule_str: str, user_tz: _tzinfo) -> datetime | None:
    """Parse RRULE in user timezone, return next trigger as UTC datetime.

    Returns None if RRULE is exhausted (no future occurrences).
    Raises ValueError/TypeError if the RRULE string is invalid.
    """
    local_now = datetime.now(user_tz)
    rule = rrulestr(rrule_str, dtstart=local_now)
    next_local = rule.after(local_now)
    if next_local is None:
        return None
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=user_tz)
    return next_local.astimezone(timezone.utc)


def user_local_now(user: User) -> datetime:
    """Get the current time in the user's configured timezone."""
    user_tz = safe_zoneinfo(user.timezone)
    return datetime.now(timezone.utc).astimezone(user_tz)


# Human-readable language names (ISO 639-1 → display name)
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "de": "German",
    "pt": "Portuguese",
}


def get_language_name(code: str) -> str:
    """Get human-readable language name from ISO 639-1 code."""
    return LANGUAGE_NAMES.get(code, code.upper())


def strip_mcp_prefix(tool_name: str) -> str:
    """Strip the MCP server prefix from a tool name."""
    return tool_name.removeprefix("mcp__langbot__")


def summarize_tool_usage(tool_names: list[str]) -> list[str]:
    """Build summary parts from a list of (stripped) tool names.

    Returns a list like ["Completed exercises", "Learned new vocabulary"].
    """
    parts: list[str] = []
    if "record_exercise_result" in tool_names:
        parts.append("Completed exercises")
    if "add_vocabulary" in tool_names:
        parts.append("Learned new vocabulary")
    if "manage_schedule" in tool_names:
        parts.append("Updated study schedule")
    if "update_preference" in tool_names:
        parts.append("Updated preferences")
    return parts


def compute_new_streak(current_streak: int, last_updated: date | None, today: date) -> int:
    """Compute the new streak value based on the gap between dates.

    Rules:
    - If last_updated == today: streak unchanged (already counted today)
    - If last_updated is None: streak starts at 1
    - If gap == 1 day: streak increments
    - If gap <= grace_days (default 2): streak unchanged (grace period, no increment)
    - If gap > grace_days and streak >= decay_threshold (30): decay to streak * 0.7
    - Otherwise: streak resets to 1
    """
    if last_updated == today:
        return current_streak
    if last_updated is None:
        return 1
    gap = (today - last_updated).days
    if gap == 1:
        return current_streak + 1
    if gap <= tuning.streak_grace_days:
        # Grace period: keep the streak but don't increment
        return current_streak
    # Beyond grace period: decay for long streaks, hard reset for short ones
    if current_streak >= tuning.streak_decay_threshold:
        return max(1, int(current_streak * tuning.streak_decay_factor))
    return 1


def is_user_admin(user: User) -> bool:
    """Check if a user has admin privileges.

    Admin status comes from two sources:
    1. The is_admin field in the database (set by other admins via Gradio)
    2. The ADMIN_TELEGRAM_IDS env var (bootstrap/fallback)
    """
    if user.is_admin:
        return True
    return user.telegram_id in settings.admin_telegram_ids


# ---------------------------------------------------------------------------
# Field timestamps — track when profile fields were set/changed
# ---------------------------------------------------------------------------

def _item_key(text: str) -> str:
    """Deterministic 8-char hex key for an array item.

    Used as JSONB key in field_timestamps to avoid duplicating full item text.
    Collision probability with ≤10 items per field is negligible (~1/4 billion).
    """
    return hashlib.sha256(text.strip().encode()).hexdigest()[:8]


def stamp_field(current_ts: dict | None, field: str, value: Any, date_str: str) -> dict:
    """Record when a field was set/changed.

    For list values: uses _item_key() hash as JSONB key, only adds date
    for items NOT already tracked (preserves original "since" date).
    For scalar values: sets field → date_str (always overwrites).

    Never mutates *current_ts*; returns a new dict.
    """
    ts = dict(current_ts or {})
    if isinstance(value, list):
        field_ts = dict(ts.get(field, {}))
        for item in value:
            key = _item_key(str(item))
            if key not in field_ts:
                field_ts[key] = date_str
        ts[field] = field_ts
    else:
        ts[field] = date_str
    return ts


def stamp_fields(current_ts: dict | None, fields: dict[str, Any], date_str: str) -> dict:
    """Batch version of stamp_field — stamps multiple fields at once."""
    ts = dict(current_ts or {})
    for field, value in fields.items():
        ts = stamp_field(ts, field, value, date_str)
    return ts


def get_item_date(ts: dict | None, field: str, item: str) -> str | None:
    """Look up the timestamp for an array item by computing its hash key.

    Returns the ISO date string or None if not tracked.
    """
    if not ts:
        return None
    field_ts = ts.get(field)
    if not isinstance(field_ts, dict):
        return None
    return field_ts.get(_item_key(item))
