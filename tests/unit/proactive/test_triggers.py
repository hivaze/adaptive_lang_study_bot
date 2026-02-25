from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from adaptive_lang_study_bot.proactive.triggers import (
    check_cards_due,
    check_incomplete_exercise,
    check_lapsed_user,
    check_post_onboarding_nudge,
    check_score_trend,
    check_streak_risk,
    check_user_inactive,
    check_weak_area_drill_due,
    check_weak_area_persistent,
)


def _make_user(**overrides):
    user = MagicMock()
    user.telegram_id = 123
    user.first_name = "Alex"
    user.streak_days = 12
    user.streak_updated_at = None
    user.last_session_at = datetime.now(timezone.utc) - timedelta(hours=14)
    user.last_activity = None
    user.recent_scores = [7, 8, 6, 9, 7]
    user.weak_areas = ["subjunctive"]
    user.target_language = "fr"
    user.timezone = "UTC"
    user.sessions_completed = 10
    user.session_history = [
        {"date": "2026-01-01", "summary": "Practice"},
        {"date": "2026-01-02", "summary": "Practice"},
        {"date": "2026-01-03", "summary": "Practice"},
    ]
    user.is_active = True
    user.onboarding_completed = True
    user.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    user.level = "A2"
    user.vocabulary_count = 120
    user.interests = ["cooking", "travel"]
    for k, v in overrides.items():
        setattr(user, k, v)
    return user


class TestStreakRisk:

    def test_no_streak_no_trigger(self):
        user = _make_user(streak_days=0)
        assert check_streak_risk(user) is None

    def test_already_studied_today_no_trigger(self):
        today = datetime.now(timezone.utc).date()
        user = _make_user(streak_updated_at=today)
        assert check_streak_risk(user) is None


class TestCardsDue:

    def test_below_threshold_no_trigger(self):
        user = _make_user()
        assert check_cards_due(user, due_count=3) is None

    def test_above_threshold_triggers(self):
        user = _make_user()
        result = check_cards_due(user, due_count=10)
        assert result is not None
        assert result["type"] == "cards_due"
        assert result["data"]["due_count"] == 10


class TestUserInactive:

    def test_short_streak_no_trigger(self):
        user = _make_user(streak_days=1)
        assert check_user_inactive(user) is None

    def test_recent_session_no_trigger(self):
        user = _make_user(
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=10),
        )
        assert check_user_inactive(user) is None

    def test_long_inactive_triggers(self):
        user = _make_user(
            streak_days=5,
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        result = check_user_inactive(user)
        assert result is not None
        assert result["type"] == "user_inactive"
        assert result["tier"] == "llm"


class TestScoreTrend:

    def test_too_few_scores_no_trigger(self):
        user = _make_user(recent_scores=[5])
        assert check_score_trend(user) is None

    def test_improving_trend(self):
        user = _make_user(recent_scores=[3, 5, 7])
        result = check_score_trend(user)
        assert result is not None
        assert result["type"] == "score_trend_improving"

    def test_declining_trend(self):
        user = _make_user(recent_scores=[8, 6, 4])
        result = check_score_trend(user)
        assert result is not None
        assert result["type"] == "score_trend_declining"

    def test_flat_scores_no_trigger(self):
        user = _make_user(recent_scores=[5, 5, 5])
        assert check_score_trend(user) is None


class TestIncompleteExercise:

    def test_no_activity_no_trigger(self):
        user = _make_user(last_activity=None)
        assert check_incomplete_exercise(user) is None

    def test_completed_no_trigger(self):
        user = _make_user(last_activity={"status": "completed", "topic": "verbs"})
        assert check_incomplete_exercise(user) is None

    def test_incomplete_too_recent_no_trigger(self):
        user = _make_user(
            last_activity={"status": "incomplete", "topic": "verbs"},
            last_session_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        assert check_incomplete_exercise(user) is None

    def test_incomplete_in_window_triggers(self):
        user = _make_user(
            last_activity={"status": "incomplete", "topic": "verbs"},
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        result = check_incomplete_exercise(user)
        assert result is not None
        assert result["type"] == "incomplete_exercise"

    def test_incomplete_too_old_no_trigger(self):
        user = _make_user(
            last_activity={"status": "incomplete", "topic": "verbs"},
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        assert check_incomplete_exercise(user) is None


class TestWeakAreaPersistent:

    def test_no_weak_areas_no_trigger(self):
        user = _make_user(weak_areas=[])
        assert check_weak_area_persistent(user) is None

    def test_insufficient_history_no_trigger(self):
        user = _make_user(weak_areas=["subjunctive"], session_history=[])
        assert check_weak_area_persistent(user) is None

    def test_just_added_topic_no_trigger(self):
        """Weak area that was the topic of the last session hasn't persisted yet."""
        user = _make_user(
            weak_areas=["subjunctive"],
            last_activity={"topic": "subjunctive"},
        )
        assert check_weak_area_persistent(user) is None

    def test_weak_area_triggers(self):
        user = _make_user(weak_areas=["subjunctive"], last_activity={"topic": "other"})
        result = check_weak_area_persistent(user)
        assert result is not None
        assert result["type"] == "weak_area_persistent"
        assert result["data"]["topic"] == "subjunctive"


class TestWeakAreaDrillDue:

    def test_no_weak_areas_no_trigger(self):
        user = _make_user(weak_areas=[])
        assert check_weak_area_drill_due(user) is None

    def test_low_sessions_no_trigger(self):
        user = _make_user(weak_areas=["subjunctive"], sessions_completed=2)
        assert check_weak_area_drill_due(user) is None

    def test_recent_session_no_trigger(self):
        user = _make_user(
            weak_areas=["subjunctive"],
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=6),
        )
        assert check_weak_area_drill_due(user) is None

    def test_good_scores_no_trigger(self):
        user = _make_user(
            weak_areas=["subjunctive"],
            recent_scores=[8, 9, 7],
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=24),
        )
        assert check_weak_area_drill_due(user) is None

    def test_conditions_met_triggers(self):
        user = _make_user(
            weak_areas=["subjunctive"],
            recent_scores=[4, 5, 3],
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=24),
        )
        result = check_weak_area_drill_due(user)
        assert result is not None
        assert result["type"] == "weak_area_drill_due"
        assert result["template_type"] == "weak_area_drill"
        assert result["data"]["topic"] == "subjunctive"


class TestPostOnboardingNudge:

    def test_not_onboarded_no_trigger(self):
        user = _make_user(onboarding_completed=False, sessions_completed=0, last_session_at=None)
        assert check_post_onboarding_nudge(user) is None

    def test_has_sessions_no_trigger(self):
        user = _make_user(
            sessions_completed=3, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        assert check_post_onboarding_nudge(user) is None

    def test_has_last_session_no_trigger(self):
        """Safety guard: last_session_at set means they engaged."""
        user = _make_user(
            sessions_completed=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=30),
            created_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        assert check_post_onboarding_nudge(user) is None

    def test_too_soon_no_trigger(self):
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=10),
        )
        assert check_post_onboarding_nudge(user) is None

    def test_24h_window_triggers(self):
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        result = check_post_onboarding_nudge(user)
        assert result is not None
        assert result["type"] == "post_onboarding_24h"
        assert result["tier"] == "template"
        assert result["data"]["name"] == "Alex"

    def test_3d_window_triggers(self):
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=60),
        )
        result = check_post_onboarding_nudge(user)
        assert result is not None
        assert result["type"] == "post_onboarding_3d"
        assert result["tier"] == "template"

    def test_7d_window_triggers_llm_tier(self):
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=120),
        )
        result = check_post_onboarding_nudge(user)
        assert result is not None
        assert result["type"] == "post_onboarding_7d"
        assert result["tier"] == "llm"

    def test_14d_window_triggers(self):
        """User 10 days after onboarding (240h) falls in 14d window (168-336h)."""
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=240),
        )
        result = check_post_onboarding_nudge(user)
        assert result is not None
        assert result["type"] == "post_onboarding_14d"
        assert result["tier"] == "template"

    def test_past_14d_no_trigger(self):
        """User 15 days after onboarding (360h) is past all windows."""
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=360),
        )
        assert check_post_onboarding_nudge(user) is None

    def test_boundary_within_24h_window(self):
        """At 47h, comfortably within the 24h window (20-48h)."""
        user = _make_user(
            sessions_completed=0, last_session_at=None,
            created_at=datetime.now(timezone.utc) - timedelta(hours=47),
        )
        result = check_post_onboarding_nudge(user)
        assert result is not None
        assert result["type"] == "post_onboarding_24h"


class TestLapsedUser:

    def test_no_sessions_no_trigger(self):
        user = _make_user(sessions_completed=0)
        assert check_lapsed_user(user) is None

    def test_no_last_session_no_trigger(self):
        user = _make_user(sessions_completed=5, last_session_at=None)
        assert check_lapsed_user(user) is None

    def test_recent_session_no_trigger(self):
        user = _make_user(
            sessions_completed=5, streak_days=1,
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        assert check_lapsed_user(user) is None

    def test_high_streak_defers_to_user_inactive(self):
        """streak >= 3 and gap >= 48h is handled by check_user_inactive."""
        user = _make_user(
            sessions_completed=5, streak_days=5,
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        assert check_lapsed_user(user) is None

    def test_low_streak_long_gap_triggers_gentle(self):
        user = _make_user(
            sessions_completed=2, streak_days=1,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        result = check_lapsed_user(user)
        assert result is not None
        assert result["type"] == "lapsed_gentle"
        assert result["tier"] == "template"

    def test_compelling_window_triggers(self):
        user = _make_user(
            sessions_completed=5, streak_days=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        result = check_lapsed_user(user)
        assert result is not None
        assert result["type"] == "lapsed_compelling"
        assert result["data"]["vocabulary_count"] == 120
        assert result["data"]["level"] == "A2"

    def test_miss_you_window_triggers_llm(self):
        user = _make_user(
            sessions_completed=10, streak_days=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        result = check_lapsed_user(user)
        assert result is not None
        assert result["type"] == "lapsed_miss_you"
        assert result["tier"] == "llm"
        assert result["data"]["gap_days"] == 10

    def test_past_21d_no_trigger(self):
        user = _make_user(
            sessions_completed=10, streak_days=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=22),
        )
        assert check_lapsed_user(user) is None

    def test_interests_passed_in_miss_you(self):
        user = _make_user(
            sessions_completed=10, streak_days=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=10),
            interests=["cooking", "travel", "music"],
        )
        result = check_lapsed_user(user)
        assert result is not None
        assert result["data"]["interests"] == "cooking, travel, music"

    def test_no_interests_empty_string(self):
        user = _make_user(
            sessions_completed=10, streak_days=0,
            last_session_at=datetime.now(timezone.utc) - timedelta(days=10),
            interests=[],
        )
        result = check_lapsed_user(user)
        assert result is not None
        assert result["data"]["interests"] == ""
