import hashlib
from datetime import datetime, timezone
from typing import Any, TypedDict

from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.enums import NotificationTier
from adaptive_lang_study_bot.utils import user_local_now

# All trigger thresholds are centralized in config.py:BotTuning.
# Access via tuning.inactivity_hours_threshold, tuning.cards_due_threshold, etc.


def _hours_since(dt: datetime, now: datetime) -> float:
    """Hours elapsed between two datetimes."""
    return (now - dt).total_seconds() / 3600


class Trigger(TypedDict):
    """Standard shape for notification trigger dicts passed between modules."""

    type: str
    template_type: str
    tier: str
    data: dict[str, Any]


def make_trigger(
    type: str,
    *,
    tier: str = NotificationTier.TEMPLATE,
    template_type: str | None = None,
    **data,
) -> Trigger:
    """Build a trigger dict with standard shape. Defaults template_type to type."""
    return {
        "type": type,
        "template_type": template_type or type,
        "tier": tier,
        "data": data,
    }


def check_streak_risk(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 1: Streak at risk if user hasn't studied today and it's evening."""
    if user.streak_days <= 0:
        return None

    local_now = user_local_now(user)
    today = local_now.date()

    # Already studied today (compare in user's local date)
    if user.streak_updated_at == today:
        return None

    # Check if it's evening in user's local timezone
    if local_now.hour < tuning.streak_risk_evening_hour:
        return None

    return make_trigger(
        "streak_risk",
        name=user.first_name,
        streak=user.streak_days,
        due_count=due_count,
    )


def check_cards_due(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 1: Cards due for review."""
    if due_count < tuning.cards_due_threshold:
        return None

    # Don't nag users who were recently active (within 2 hours)
    if user.last_session_at is not None:
        gap = (datetime.now(timezone.utc) - user.last_session_at).total_seconds()
        if gap < tuning.recent_activity_seconds:
            return None

    return make_trigger("cards_due", name=user.first_name, due_count=due_count)


def check_user_inactive(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 1: User inactive for 48+ hours with active streak."""
    if user.streak_days < 3:
        return None

    if user.last_session_at is None:
        return None

    now = datetime.now(timezone.utc)
    gap_hours = _hours_since(user.last_session_at, now)

    if gap_hours < tuning.inactivity_hours_threshold:
        return None

    return make_trigger(
        "user_inactive",
        tier=NotificationTier.LLM,
        name=user.first_name,
        streak=user.streak_days,
        target_language=user.target_language,
        gap_hours=round(gap_hours, 1),
    )


def check_weak_area_persistent(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 2: A weak area has persisted across multiple sessions.

    Requires at least 3 entries in session_history and that the weak area
    was not just added in the most recent session (i.e. it actually persisted).
    """
    if not user.weak_areas:
        return None

    # Require enough session history to establish persistence
    history = user.session_history or []
    if len(history) < 3:
        return None

    # Pick the first weak area not covered in the most recent session —
    # skip topics that were likely just added and haven't persisted yet.
    last = user.last_activity or {}
    last_topic = last.get("topic")
    topic = next((t for t in user.weak_areas if t != last_topic), None)
    if topic is None:
        return None

    return make_trigger("weak_area_persistent", name=user.first_name, topic=topic)


def check_score_trend(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 2: 3+ consecutive scores improving or declining."""
    scores = user.recent_scores or []
    if len(scores) < 3:
        return None

    last_3 = scores[-3:]

    # Check improving (strictly increasing)
    if all(last_3[i] < last_3[i + 1] for i in range(len(last_3) - 1)):
        return make_trigger("score_trend_improving", name=user.first_name)

    # Check declining (strictly decreasing)
    if all(last_3[i] > last_3[i + 1] for i in range(len(last_3) - 1)):
        return make_trigger("score_trend_declining", name=user.first_name)

    return None


def check_incomplete_exercise(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 2: Last exercise was incomplete and 1-24 hours have passed."""
    last = user.last_activity
    if not last or last.get("status") != "incomplete":
        return None

    # Sessions that ended via idle_timeout after substantive work (≥2 exercises
    # or agent-initiated wrap-up) are effectively completed — don't nag.
    if last.get("close_reason") == "idle_timeout" and (
        last.get("exercise_count", 0) >= 2 or last.get("agent_stopped")
    ):
        return None

    if user.last_session_at is None:
        return None

    now = datetime.now(timezone.utc)
    gap_hours = _hours_since(user.last_session_at, now)

    if gap_hours < 1 or gap_hours > 24:
        return None

    topic = last.get("topic")
    if not topic:
        return None

    return make_trigger(
        "incomplete_exercise",
        name=user.first_name,
        topic=topic,
    )


def check_weak_area_drill_due(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 2: Suggest a targeted drill when weak areas persist and user hasn't practiced recently."""
    if not user.weak_areas:
        return None

    if user.sessions_completed < 3:
        return None

    if user.last_session_at is None:
        return None

    now = datetime.now(timezone.utc)
    gap_hours = _hours_since(user.last_session_at, now)

    if gap_hours < 12:
        return None

    # Only trigger if recent scores aren't all good (some struggle remains)
    recent_3 = (user.recent_scores or [])[-3:]
    if recent_3 and all(s > 6 for s in recent_3):
        return None

    # Skip the topic from the most recent session (may have just been addressed)
    last = user.last_activity or {}
    last_topic = last.get("topic")
    topic = next((t for t in user.weak_areas if t != last_topic), None)
    if topic is None:
        return None

    return make_trigger(
        "weak_area_drill_due",
        template_type="weak_area_drill",
        name=user.first_name,
        topic=topic,
        gap_hours=round(gap_hours, 1),
    )


def check_post_onboarding_nudge(user: User, *, due_count: int = 0) -> Trigger | None:
    """Re-engagement: user completed onboarding but never started a real session.

    Escalation via distinct notification types (each gets its own dedup slot):
      20-48h  after created_at -> post_onboarding_24h (template)
      48-72h                   -> post_onboarding_3d  (template)
      72-168h (3-7 days)       -> post_onboarding_7d  (LLM, final attempt)
      >168h                    -> None (stop nudging forever)
    """
    if not user.onboarding_completed:
        return None

    if user.sessions_completed > 0:
        return None

    if user.last_session_at is not None:
        return None

    now = datetime.now(timezone.utc)
    gap_hours = _hours_since(user.created_at, now)

    if gap_hours < tuning.post_onboarding_min_hours:
        return None

    target_language = user.target_language

    if gap_hours <= tuning.post_onboarding_24h_max_hours:
        return make_trigger(
            "post_onboarding_24h",
            name=user.first_name,
            target_language=target_language,
        )

    if gap_hours <= tuning.post_onboarding_3d_max_hours:
        return make_trigger(
            "post_onboarding_3d",
            name=user.first_name,
            target_language=target_language,
        )

    if gap_hours <= tuning.post_onboarding_7d_max_hours:
        return make_trigger(
            "post_onboarding_7d",
            tier=NotificationTier.LLM,
            name=user.first_name,
            target_language=target_language,
        )

    if gap_hours <= tuning.post_onboarding_max_hours:
        return make_trigger(
            "post_onboarding_14d",
            name=user.first_name,
            target_language=target_language,
        )

    return None


def check_dormant_user(user: User, *, due_count: int = 0) -> Trigger | None:
    """Re-engagement: periodic nudge for dormant users (21-45 days inactive).

    Fires approximately weekly for users who have gone silent beyond the
    lapsed_miss_you window (21 days) but haven't been gone so long that
    we stop entirely (45 days).  Uses modular arithmetic on gap_days to
    approximate a weekly cadence within the tick interval.

    Dedup key: dormant_weekly with TTL of (interval - 1) days.
    """
    # Allow onboarded-but-never-engaged users (completed setup, 0 sessions)
    # to receive dormant nudges after post_onboarding_max_hours expires.
    # Without this, they're permanently abandoned after the post-onboarding
    # nudge sequence ends.
    if user.sessions_completed < 1 and not user.onboarding_completed:
        return None

    # Use last_session_at for users with sessions, created_at for
    # onboarded-but-never-engaged users.
    reference_dt = user.last_session_at or (
        user.created_at if user.onboarding_completed else None
    )
    if reference_dt is None:
        return None

    now = datetime.now(timezone.utc)
    gap_days = (now - reference_dt).total_seconds() / 86400

    if gap_days <= tuning.lapsed_miss_you_max_days:
        return None

    if gap_days >= tuning.dormant_periodic_max_days:
        return None

    # Fire approximately every `dormant_periodic_interval_days` days.
    # Use round(gap_days) to avoid both floating-point modulo imprecision
    # and int() truncation (e.g. 20.9 truncates to 20, 20 % 7 == 6 → missed).
    if round(gap_days) % tuning.dormant_periodic_interval_days != 0:
        return None

    return make_trigger(
        "dormant_weekly",
        name=user.first_name,
        target_language=user.target_language,
        dedup_ttl=(tuning.dormant_periodic_interval_days - 1) * 86400,
    )


def check_lapsed_user(user: User, *, due_count: int = 0) -> Trigger | None:
    """Re-engagement: user had sessions but went silent for days/weeks.

    Covers users that check_user_inactive misses (streak < 3 or not yet 48h).
    Explicitly defers to check_user_inactive when streak >= 3 and gap >= 48h.

    Escalation:
      2-4 days  -> lapsed_gentle     (template)
      4-8 days  -> lapsed_compelling  (template, mentions progress)
      8-15 days -> lapsed_miss_you    (LLM, final attempt)
      >15 days  -> None (stop)
    """
    if user.sessions_completed < 1:
        return None

    if user.last_session_at is None:
        return None

    now = datetime.now(timezone.utc)
    gap_hours = _hours_since(user.last_session_at, now)
    gap_days = gap_hours / 24

    if gap_days < tuning.lapsed_gentle_min_days:
        return None

    # Defer to check_user_inactive for users it already covers
    if user.streak_days >= 3 and gap_hours >= tuning.inactivity_hours_threshold:
        return None

    target_language = user.target_language

    if gap_days < tuning.lapsed_gentle_max_days:
        return make_trigger(
            "lapsed_gentle",
            name=user.first_name,
            target_language=target_language,
        )

    if gap_days < tuning.lapsed_compelling_max_days:
        return make_trigger(
            "lapsed_compelling",
            name=user.first_name,
            target_language=target_language,
            level=user.level,
            vocabulary_count=user.vocabulary_count,
            sessions_completed=user.sessions_completed,
        )

    if gap_days < tuning.lapsed_miss_you_max_days:
        interests = user.interests or []
        return make_trigger(
            "lapsed_miss_you",
            tier=NotificationTier.LLM,
            name=user.first_name,
            target_language=target_language,
            level=user.level,
            vocabulary_count=user.vocabulary_count,
            sessions_completed=user.sessions_completed,
            interests=", ".join(interests[:3]),
            gap_days=round(gap_days),
        )

    return None


def check_progress_celebration(user: User, *, due_count: int = 0) -> Trigger | None:
    """Tier 2: Periodic positive reinforcement for active learners.

    Fires for users who have completed enough sessions, were active recently,
    and meet at least one positive metric (good avg scores, growing vocab,
    or active streak).  A deterministic daily gate (~33% chance) prevents
    notification fatigue.
    """
    if user.sessions_completed < tuning.progress_celebration_min_sessions:
        return None

    if user.last_session_at is None:
        return None

    now = datetime.now(timezone.utc)
    gap_days = (now - user.last_session_at).total_seconds() / 86400
    if gap_days > tuning.progress_celebration_max_inactive_days:
        return None

    # Deterministic daily gate: hash(user_id + local_date) → stable within
    # a single day but varies across days and users.
    local_now = user_local_now(user)
    date_str = local_now.date().isoformat()
    seed = hashlib.md5(f"{user.telegram_id}:{date_str}".encode()).hexdigest()  # noqa: S324
    roll = int(seed[:4], 16)  # 0..65535
    threshold = int(65536 * tuning.progress_celebration_chance_pct / 100)
    if roll >= threshold:
        return None

    # Require at least one celebration-worthy metric
    scores = user.recent_scores or []
    last_5 = scores[-5:]
    avg_score = sum(last_5) / len(last_5) if len(last_5) >= 3 else 0.0
    vocab_count = user.vocabulary_count or 0
    streak = user.streak_days or 0

    has_good_scores = avg_score >= tuning.progress_celebration_good_avg and len(last_5) >= 3
    has_vocab_growth = vocab_count >= tuning.progress_celebration_min_vocab
    has_streak = streak >= tuning.progress_celebration_min_streak

    if not (has_good_scores or has_vocab_growth or has_streak):
        return None

    data: dict[str, Any] = {"name": user.first_name, "sessions_completed": user.sessions_completed}
    if has_good_scores:
        data["avg_score"] = round(avg_score, 1)
    if has_vocab_growth:
        data["vocab_count"] = vocab_count
    if has_streak:
        data["streak"] = streak

    return make_trigger("progress_celebration", **data)


# All trigger functions in evaluation order.
# Every function has the uniform signature: (user, *, due_count) -> Trigger | None
# so they can be called in a loop.  Not all functions use due_count —
# it is only consumed by check_streak_risk (template data) and
# check_cards_due (threshold check + template data).
ALL_TRIGGERS: list = [
    # Tier 1 — Critical (active users)
    check_streak_risk,
    check_cards_due,
    check_user_inactive,
    # Tier 1.5 — Re-engagement (silent/lapsed users)
    check_post_onboarding_nudge,
    check_lapsed_user,
    check_dormant_user,
    # Tier 2 — Learning path
    check_weak_area_persistent,
    check_weak_area_drill_due,
    check_score_trend,
    check_incomplete_exercise,
    check_progress_celebration,
]
