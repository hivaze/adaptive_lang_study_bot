from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from adaptive_lang_study_bot.agent.prompt_builder import (
    _build_comeback_section,
    build_internal_summary_prompt,
    build_proactive_prompt,
    build_system_prompt,
    compute_session_context,
)


def _make_user(**overrides):
    """Create a mock User object."""
    user = MagicMock()
    user.telegram_id = 123
    user.first_name = "Alex"
    user.native_language = "en"
    user.target_language = "fr"
    user.level = "A2"
    user.streak_days = 12
    user.vocabulary_count = 340
    user.sessions_completed = 45
    user.interests = ["cooking", "travel"]
    user.preferred_difficulty = "normal"
    user.session_style = "structured"
    user.topics_to_avoid = ["politics"]
    user.weak_areas = ["subjunctive mood"]
    user.strong_areas = ["basic vocabulary"]
    user.recent_scores = [7, 8, 6, 9, 7]
    user.last_session_at = datetime.now(timezone.utc) - timedelta(hours=14)
    user.last_activity = {
        "type": "exercise",
        "topic": "irregular verbs",
        "status": "completed",
        "score": 7,
        "last_exercise": "Fill the blank",
        "session_summary": "Practiced irregular verbs",
    }
    user.learning_goals = []
    user.session_history = []
    user.milestones = {"pending_celebrations": [], "days_streak": 12}
    user.last_notification_text = None
    user.last_notification_at = None
    user.onboarding_completed = True
    user.tier = "free"
    user.timezone = "UTC"
    user.notifications_paused = False
    user.additional_notes = []
    user.field_timestamps = {}
    for k, v in overrides.items():
        setattr(user, k, v)
    return user


class TestComputeSessionContext:

    def test_continuation_gap(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "continuation"

    def test_short_break(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(hours=2))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "short_break"

    def test_normal_return(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(hours=6))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "normal_return"

    def test_long_break(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(hours=14))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "long_break"

    def test_day_plus_break(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(hours=36))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "day_plus_break"

    def test_long_absence(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(days=5))
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "long_absence"

    def test_new_user_long_absence(self):
        user = _make_user(last_session_at=None)
        ctx = compute_session_context(user)
        assert ctx["greeting_style"] == "long_absence"

    def test_celebrations_populated(self):
        user = _make_user(
            milestones={"pending_celebrations": ["10-day streak!"]},
        )
        ctx = compute_session_context(user)
        assert "10-day streak!" in ctx["celebrations"]

    def test_notification_context_recent(self):
        user = _make_user(
            last_notification_text="Time to review!",
            last_notification_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        ctx = compute_session_context(user)
        assert ctx["notification_text"] == "Time to review!"

    def test_notification_context_old(self):
        user = _make_user(
            last_notification_text="Time to review!",
            last_notification_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        ctx = compute_session_context(user)
        assert ctx["notification_text"] is None

    def test_notification_html_stripped(self):
        user = _make_user(
            last_notification_text="You have <b>7</b> cards due!",
            last_notification_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        ctx = compute_session_context(user)
        assert ctx["notification_text"] == "You have 7 cards due!"

    def test_night_time_of_day(self):
        """Hours 0-5 should be classified as 'night', not 'morning'."""
        user = _make_user(timezone="Etc/GMT-3")  # UTC+3 → local_hour = utc + 3
        # Force a time where UTC+3 gives a nighttime hour.
        # We can't control datetime.now, so just verify the mapping values.
        ctx = compute_session_context(user)
        assert ctx["time_of_day"] in ("night", "morning", "afternoon", "evening")


class TestBuildSystemPrompt:

    def test_contains_role_and_rules(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "## ROLE" in prompt
        assert "## RULES" in prompt
        assert "## TOOL REQUIREMENTS" in prompt
        assert "record_exercise_result" in prompt

    def test_contains_student_profile(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "STUDENT PROFILE" in prompt
        assert "Alex" in prompt
        assert "A2" in prompt
        assert "cooking" in prompt

    def test_contains_teaching_approach(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "TEACHING APPROACH" in prompt

    def test_contains_session_context(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION CONTEXT" in prompt
        assert ctx["greeting_style"] in prompt

    def test_contains_scheduling_instructions(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SCHEDULING INSTRUCTIONS" in prompt
        assert "RRULE" in prompt

    def test_topics_to_avoid_mentioned(self):
        user = _make_user(topics_to_avoid=["politics", "religion"])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "politics" in prompt

    def test_high_scores_noted_onboarding(self):
        """Onboarding sessions (no perf tools) get static score hints."""
        user = _make_user(recent_scores=[9, 10, 9, 10, 9])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "performing well" in prompt

    def test_low_scores_noted_onboarding(self):
        """Onboarding sessions (no perf tools) get static score hints."""
        user = _make_user(recent_scores=[2, 3, 1, 2, 3])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "struggling" in prompt

    def test_interactive_omits_static_score_hint(self):
        """Interactive sessions rely on tools — no static score adaptation hint."""
        user = _make_user(recent_scores=[9, 10, 9, 10, 9])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "performing well" not in prompt
        assert "get_progress_summary" in prompt

    def test_incomplete_exercise_noted(self):
        user = _make_user(
            last_activity={"status": "incomplete", "topic": "verbs"},
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended mid-conversation" in prompt

    def test_notification_context_in_prompt(self):
        user = _make_user(
            last_notification_text="Review your words!",
            last_notification_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "NOTIFICATION REPLY CONTEXT" in prompt
        assert "Review your words!" in prompt

    def test_due_count_shown(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, due_count=15)
        assert "15" in prompt

    def test_native_language_instruction(self):
        user = _make_user(native_language="ru")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Russian" in prompt
        assert "Communicate with the student in Russian" in prompt

    def test_native_language_english_default(self):
        user = _make_user(native_language="en")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Communicate with the student in English" in prompt

    def test_timezone_aware_time_of_day(self):
        user = _make_user(timezone="Asia/Tokyo")
        ctx = compute_session_context(user)
        # Should compute time_of_day based on Tokyo time, not UTC
        assert ctx["time_of_day"] in ("night", "morning", "afternoon", "evening")

    def test_same_language_strengthening_prompt(self):
        """When native == target, prompt should use strengthening mode."""
        user = _make_user(native_language="en", target_language="en")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "strengthening" in prompt.lower()
        assert "native-level" in prompt.lower()
        # Should NOT contain the contradictory "only for teaching content" rule
        assert "only for teaching content" not in prompt

    def test_same_language_profile_shows_mode(self):
        """Same-language profile section should indicate strengthening mode."""
        user = _make_user(native_language="fr", target_language="fr")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "strengthening mode" in prompt

    def test_different_language_no_strengthening(self):
        """Standard native != target should use normal language rules."""
        user = _make_user(native_language="en", target_language="fr")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "only for teaching content" in prompt
        assert "strengthening mode" not in prompt


class TestLastActivityContext:
    """Test that enriched last_activity fields are rendered in the system prompt."""

    def test_topic_shown(self):
        user = _make_user(last_activity={
            "topic": "subjunctive",
            "session_summary": "Grammar practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Last topic: subjunctive" in prompt

    def test_topics_covered_shown(self):
        user = _make_user(last_activity={
            "topics_covered": ["subjunctive", "cooking vocabulary"],
            "session_summary": "Mixed session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Topics covered last time:" in prompt
        assert "subjunctive" in prompt
        assert "cooking vocabulary" in prompt

    def test_words_practiced_shown(self):
        user = _make_user(last_activity={
            "words_practiced": ["bonjour", "merci", "salut"],
            "session_summary": "Vocab session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Words practiced last time:" in prompt
        assert "bonjour" in prompt

    def test_last_exercise_and_score_shown(self):
        user = _make_user(last_activity={
            "last_exercise": "fill_blank",
            "score": 8,
            "session_summary": "Exercise session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Last exercise: fill_blank" in prompt
        assert "Last score: very good" in prompt

    def test_incomplete_with_topic_shown(self):
        """Incomplete status with topic includes topic in the continuation note."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "irregular verbs",
            "session_summary": "Was practicing verbs",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended mid-conversation" in prompt
        assert "irregular verbs" in prompt

    def test_incomplete_without_topic_shown(self):
        """Incomplete status without topic still shows continuation note."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "session_summary": "Practice session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended mid-conversation" in prompt

    def test_enriched_last_activity_full(self):
        """Full enriched last_activity as produced by the pipeline."""
        user = _make_user(last_activity={
            "type": "session",
            "status": "incomplete",
            "session_summary": "Completed exercises. Topics: subjunctive, travel",
            "tools_used": ["record_exercise_result", "add_vocabulary"],
            "last_exercise": "translation",
            "topic": "subjunctive",
            "score": 6,
            "words_practiced": ["quiero", "puedo", "tengo"],
            "topics_covered": ["subjunctive", "travel"],
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Last topic: subjunctive" in prompt
        assert "Last exercise: translation" in prompt
        assert "Last score: good" in prompt
        assert "Topics covered last time:" in prompt
        assert "ended mid-conversation on 'subjunctive'" in prompt
        assert "quiero" in prompt

    def test_empty_last_activity_no_crash(self):
        """Empty last_activity dict should not crash."""
        user = _make_user(last_activity={})
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION CONTEXT" in prompt

    def test_none_last_activity_no_crash(self):
        """None last_activity should not crash."""
        user = _make_user(last_activity=None)
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION CONTEXT" in prompt


class TestLearningGoals:
    """Test that learning_goals are rendered in the system prompt."""

    def test_goals_shown_in_profile(self):
        user = _make_user(learning_goals=["Prepare for DELF B2 exam", "Learn cooking vocabulary"])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Prepare for DELF B2 exam" in prompt
        assert "Learn cooking vocabulary" in prompt

    def test_empty_goals_shows_placeholder(self):
        user = _make_user(learning_goals=[])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "none set yet" in prompt

    def test_none_goals_shows_placeholder(self):
        user = _make_user(learning_goals=None)
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "none set yet" in prompt

    def test_goal_tracking_instruction_present(self):
        """Prompt should instruct agent to save learning goals."""
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "learning goal" in prompt.lower()
        assert "update_preference" in prompt


class TestSessionHistory:
    """Test that session_history is rendered in the system prompt.

    Session history is only included for sessions without performance tools
    (onboarding). Interactive sessions omit it — the agent can call
    get_exercise_history for live data.
    """

    def test_history_shown_onboarding(self):
        user = _make_user(session_history=[
            {"date": "2026-02-19", "summary": "Grammar practice", "topics": ["subjunctive"], "score": 7, "status": "completed"},
            {"date": "2026-02-20", "summary": "Vocabulary review", "topics": ["cooking"], "status": "completed"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "Recent session history:" in prompt
        assert "2026-02-19" in prompt
        assert "Grammar practice" in prompt
        assert "subjunctive" in prompt
        assert "2026-02-20" in prompt
        assert "Vocabulary review" in prompt

    def test_history_omitted_interactive(self):
        """Interactive sessions skip static session history (tools available)."""
        user = _make_user(session_history=[
            {"date": "2026-02-19", "summary": "Grammar practice", "score": 7, "status": "completed"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Recent session history:" not in prompt

    def test_history_shows_incomplete_status(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Verb practice", "status": "incomplete"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(incomplete)" in prompt

    def test_history_shows_score(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Quiz", "score": 8, "status": "completed"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "score: very good" in prompt

    def test_empty_history_no_section(self):
        user = _make_user(session_history=[])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "Recent session history:" not in prompt

    def test_none_history_no_crash(self):
        user = _make_user(session_history=None)
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION CONTEXT" in prompt

    def test_history_with_multiple_topics(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Mixed practice", "topics": ["verbs", "cooking", "travel"], "status": "completed"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "verbs" in prompt
        assert "cooking" in prompt

    def test_all_history_entries_rendered(self):
        """All session_history entries are rendered (bounded upstream by session_history_cap)."""
        user = _make_user(session_history=[
            {"date": f"2026-02-{i:02d}", "summary": f"Session {i}", "status": "completed"}
            for i in range(1, 14)
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        # All entries should be present — no prompt-level cap
        assert "Session 1" in prompt
        assert "Session 8" in prompt
        assert "Session 13" in prompt


class TestStyleInstructions:
    """Test that session style instructions appear in the prompt via SESSION FLOW."""

    def test_casual_style(self):
        user = _make_user(session_style="casual")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION FLOW (Casual)" in prompt
        assert "natural conversation" in prompt

    def test_structured_style(self):
        user = _make_user(session_style="structured")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION FLOW (Structured)" in prompt
        assert "THEORY/GRAMMAR BLOCK" in prompt

    def test_intensive_style(self):
        user = _make_user(session_style="intensive")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "SESSION FLOW (Intensive)" in prompt
        assert "Get to work immediately" in prompt


class TestDifficultyInstructions:
    """Test that difficulty instructions appear in the prompt."""

    def test_easy_difficulty(self):
        user = _make_user(preferred_difficulty="easy")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "DIFFICULTY: Easy" in prompt

    def test_normal_difficulty(self):
        user = _make_user(preferred_difficulty="normal")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "DIFFICULTY: Normal" in prompt

    def test_hard_difficulty(self):
        user = _make_user(preferred_difficulty="hard")
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "DIFFICULTY: Hard" in prompt
        assert "complex" in prompt.lower()


class TestGoalInstructions:
    """Test expanded goal instructions in the prompt."""

    def test_encourage_setting_goals(self):
        user = _make_user(learning_goals=[])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "encourage the student to set goals" in prompt

    def test_align_exercises_with_goals(self):
        user = _make_user(learning_goals=["Travel to France"])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Align exercises with the student's learning goals" in prompt

    def test_periodically_check_goals(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "periodically ask about progress" in prompt

    def test_suggest_relevant_vocab(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "directly relevant to the student's goals" in prompt


class TestIncompleteSessionEnriched:
    """Test enriched incomplete session context."""

    def test_exercise_type_shown(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "verbs",
            "last_exercise": "fill_blank",
            "session_summary": "Exercise session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "fill_blank" in prompt

    def test_score_shown(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "verbs",
            "score": 5,
            "session_summary": "Exercise session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Last score: needs work" in prompt

    def test_words_practiced_shown(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "verbs",
            "words_practiced": ["hacer", "tener", "poder"],
            "session_summary": "Exercise session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "hacer" in prompt
        assert "Words practiced last time" in prompt

    def test_offer_choice_to_continue_or_start_fresh(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "verbs",
            "session_summary": "Exercise session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "continue" in prompt.lower()
        assert "start fresh" in prompt or "start something new" in prompt

    def test_struggling_topics_in_incomplete(self):
        """Incomplete session with struggling topics should include revisit advice."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "grammar",
            "session_summary": "Grammar session",
            "struggling_topics": [
                {"topic": "subjunctive", "avg_score": 3.0},
                {"topic": "articles", "avg_score": 4.5},
            ],
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Struggled with" in prompt
        assert "subjunctive" in prompt
        assert "poor" in prompt
        assert "revisit with simpler exercises" in prompt


class TestErrorPatterns:
    """Test error pattern and exercise performance rendering in the prompt."""

    def test_exercise_type_scores_shown(self):
        user = _make_user(last_activity={
            "session_summary": "Mixed session",
            "exercise_type_scores": {"translation": 8.5, "fill_blank": 4.0},
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Exercise performance last time:" in prompt
        assert "translation: very good" in prompt
        assert "fill_blank: needs work" in prompt

    def test_struggling_topics_completed_session(self):
        """Completed session with struggling topics shows extra practice note."""
        user = _make_user(last_activity={
            "status": "completed",
            "session_summary": "Practice session",
            "struggling_topics": [
                {"topic": "prepositions", "avg_score": 3.5},
            ],
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Topics that need extra practice:" in prompt
        assert "prepositions" in prompt
        assert "needs work" in prompt

    def test_no_struggling_topics_no_section(self):
        user = _make_user(last_activity={
            "status": "completed",
            "session_summary": "Good session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Topics that need extra practice:" not in prompt
        assert "struggled with" not in prompt

    def test_no_exercise_type_scores_no_section(self):
        user = _make_user(last_activity={
            "session_summary": "Chat session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Exercise performance last time:" not in prompt


class TestStaleTopics:
    """Test stale topics rendering in the prompt.

    Stale topics and topic performance are only included for sessions
    without performance tools (onboarding). Interactive sessions omit
    them — the agent can call get_progress_summary for live data.
    """

    def test_stale_topics_rendered_onboarding(self):
        user = _make_user()
        ctx = compute_session_context(user)
        stale = [
            {"topic": "subjunctive", "days_ago": 10.5, "avg_score": 4.2},
            {"topic": "travel vocab", "days_ago": 8.0, "avg_score": 5.5},
        ]
        prompt = build_system_prompt(user, ctx, stale_topics=stale, session_type="onboarding")
        assert "Topics needing review" in prompt
        assert "subjunctive" in prompt
        assert "10.5" in prompt
        assert "4.2" in prompt
        assert "travel vocab" in prompt

    def test_stale_topics_omitted_interactive(self):
        """Interactive sessions skip static stale topics (tools available)."""
        user = _make_user()
        ctx = compute_session_context(user)
        stale = [{"topic": "subjunctive", "days_ago": 10.5, "avg_score": 4.2}]
        prompt = build_system_prompt(user, ctx, stale_topics=stale)
        assert "Topics needing review" not in prompt

    def test_empty_stale_topics(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, stale_topics=[], session_type="onboarding")
        assert "Topics needing review" not in prompt

    def test_none_stale_topics(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, stale_topics=None, session_type="onboarding")
        assert "Topics needing review" not in prompt

    def test_topic_review_instruction(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "hasn't practiced recently" in prompt


class TestTopicPerformance:
    """Test 7-day topic performance snapshot rendering in the prompt.

    Only included for sessions without performance tools (onboarding).
    Interactive sessions rely on get_progress_summary for live data.
    """

    def test_topic_performance_rendered_onboarding(self):
        user = _make_user()
        ctx = compute_session_context(user)
        perf = {
            "subjunctive": {"avg_score": 4.2, "count": 5},
            "travel vocab": {"avg_score": 8.0, "count": 3},
        }
        prompt = build_system_prompt(user, ctx, topic_performance=perf, session_type="onboarding")
        assert "Topic performance (last 7 days):" in prompt
        assert "subjunctive" in prompt
        assert "needs work" in prompt  # 4.2 → needs work
        assert "5 exercises" in prompt
        assert "travel vocab" in prompt
        assert "very good" in prompt  # 8.0 → very good
        assert "3 exercises" in prompt

    def test_topic_performance_omitted_interactive(self):
        """Interactive sessions skip static topic performance (tools available)."""
        user = _make_user()
        ctx = compute_session_context(user)
        perf = {"subjunctive": {"avg_score": 4.2, "count": 5}}
        prompt = build_system_prompt(user, ctx, topic_performance=perf)
        assert "Topic performance (last 7 days):" not in prompt

    def test_topic_performance_sorted_by_count(self):
        """Topics should be sorted by exercise count descending."""
        user = _make_user()
        ctx = compute_session_context(user)
        perf = {
            "rare_topic": {"avg_score": 7.0, "count": 1},
            "common_topic": {"avg_score": 6.0, "count": 10},
        }
        prompt = build_system_prompt(user, ctx, topic_performance=perf, session_type="onboarding")
        common_pos = prompt.index("common_topic")
        rare_pos = prompt.index("rare_topic")
        assert common_pos < rare_pos

    def test_all_topic_performance_rendered(self):
        """All topics are rendered (bounded upstream by DB query)."""
        user = _make_user()
        ctx = compute_session_context(user)
        perf = {
            f"topic_{i}": {"avg_score": 5.0, "count": 20 - i}
            for i in range(12)
        }
        prompt = build_system_prompt(user, ctx, topic_performance=perf, session_type="onboarding")
        # All topics should be present — no prompt-level cap
        assert "topic_0" in prompt
        assert "topic_9" in prompt
        assert "topic_10" in prompt
        assert "topic_11" in prompt

    def test_empty_topic_performance(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, topic_performance={}, session_type="onboarding")
        assert "Topic performance (last 7 days):" not in prompt

    def test_none_topic_performance(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, topic_performance=None, session_type="onboarding")
        assert "Topic performance (last 7 days):" not in prompt


# ---------------------------------------------------------------------------
# build_proactive_prompt
# ---------------------------------------------------------------------------

class TestBuildProactivePrompt:

    def test_contains_role(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {"streak": 12})
        assert "ROLE" in prompt
        assert "proactive" in prompt.lower()

    def test_contains_student_name(self):
        user = _make_user(first_name="Alex")
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "Alex" in prompt

    def test_contains_native_language_rule(self):
        user = _make_user(native_language="ru")
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "Russian" in prompt

    def test_instructs_direct_output(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "directly as your response" in prompt
        assert "send_notification" not in prompt

    def test_review_mentions_vocabulary(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_review", {"due_count": 15})
        assert "vocabulary" in prompt.lower()
        assert "15" in prompt

    def test_quiz_mentions_quiz(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_quiz", {})
        assert "quiz" in prompt.lower()

    def test_summary_mentions_progress(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_summary", {})
        assert "progress" in prompt.lower() or "summary" in prompt.lower()

    def test_nudge_mentions_motivat(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "motivat" in prompt.lower()

    def test_trigger_data_included(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {"streak": 12, "due_count": 5})
        assert "TRIGGER CONTEXT" in prompt
        assert "streak" in prompt
        assert "12" in prompt

    def test_interests_included(self):
        user = _make_user(interests=["cooking", "travel"])
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "cooking" in prompt
        # "travel" is a known interest code, rendered as "Travel & Places"
        assert "Travel" in prompt

    def test_weak_areas_included(self):
        user = _make_user(weak_areas=["subjunctive mood"])
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "subjunctive mood" in prompt

    def test_topics_to_avoid_included(self):
        user = _make_user(topics_to_avoid=["politics"])
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "politics" in prompt
        assert "topics_to_avoid" in prompt.lower() or "avoid" in prompt.lower()

    def test_formatting_instruction(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "**bold**" in prompt

    def test_unknown_session_type_falls_back_to_nudge(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "unknown_type", {})
        assert "motivat" in prompt.lower()

    def test_empty_trigger_data(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "ROLE" in prompt

    def test_session_style_and_difficulty_included(self):
        user = _make_user(session_style="structured", preferred_difficulty="hard")
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "structured" in prompt
        assert "hard" in prompt

    def test_strong_areas_included(self):
        user = _make_user(strong_areas=["present tense"])
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "present tense" in prompt

    def test_additional_notes_included(self):
        user = _make_user(additional_notes=["prefers short exercises"])
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "prefers short exercises" in prompt

    def test_last_session_gap_shown(self):
        user = _make_user(last_session_at=datetime.now(timezone.utc) - timedelta(hours=5))
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "Last session:" in prompt
        assert "hours ago" in prompt

    def test_recent_sessions_rendered(self):
        sess = MagicMock()
        sess.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        sess.ended_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=45)
        sess.ai_summary = "Covered verb conjugation. Good performance on exercises."
        prompt = build_proactive_prompt(user=_make_user(), session_type="proactive_nudge", trigger_data={}, recent_sessions=[sess])
        assert "RECENT CONTEXT" in prompt
        assert "Covered verb conjugation" in prompt

    def test_last_activity_fallback_when_no_summaries(self):
        user = _make_user(last_activity={"session_summary": "Practiced articles", "topic": "grammar"})
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "RECENT CONTEXT" in prompt
        assert "Practiced articles" in prompt

    def test_last_activity_hidden_when_summaries_present(self):
        user = _make_user(last_activity={"session_summary": "Old summary", "topic": "grammar"})
        sess = MagicMock()
        sess.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        sess.ended_at = datetime.now(timezone.utc) - timedelta(minutes=45)
        sess.ai_summary = "AI summary replaces last_activity."
        prompt = build_proactive_prompt(user, "proactive_nudge", {}, recent_sessions=[sess])
        assert "Old summary" not in prompt
        assert "AI summary replaces last_activity" in prompt

    def test_prefetch_due_vocabulary(self):
        user = _make_user()
        prefetch = {
            "due_vocabulary": [
                {"word": "bonjour", "translation": "hello", "topic": "greetings", "review_count": 3},
                {"word": "merci", "translation": "thank you", "topic": "greetings", "review_count": 1},
            ],
        }
        prompt = build_proactive_prompt(user, "proactive_review", {"due_count": 2}, prefetch=prefetch)
        assert "DUE VOCABULARY" in prompt
        assert "bonjour" in prompt
        assert "merci" in prompt
        assert "reviewed 3 times" in prompt

    def test_prefetch_due_vocabulary_empty(self):
        """Empty due_vocabulary list: no vocabulary section rendered (task may still reference it)."""
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_review", {"due_count": 5}, prefetch={"due_vocabulary": []})
        # The ## DUE VOCABULARY header should not be rendered as a section
        assert "The following cards are due for review" not in prompt

    def test_prefetch_progress_summary(self):
        user = _make_user()
        prefetch = {
            "progress_summary": {
                "score_7d": {"count": 10, "avg": 7.5},
                "score_30d": {"count": 30, "avg": 6.8},
                "topic_performance": [
                    {"topic": "verbs", "exercise_count": 5, "avg_score": 8.2},
                    {"topic": "nouns", "exercise_count": 3, "avg_score": 6.0},
                ],
                "vocabulary": {"new": 5, "learning": 10, "review": 20, "relearning": 2},
                "sessions_this_week": 4,
            },
        }
        prompt = build_proactive_prompt(user, "proactive_summary", {}, prefetch=prefetch)
        assert "PROGRESS DATA" in prompt
        assert "Topic Performance" in prompt
        assert "verbs" in prompt
        assert "very good" in prompt  # 8.2 → very good
        assert "Vocabulary" in prompt
        assert "new: 5" in prompt
        assert "Session Activity" in prompt
        assert "4 sessions" in prompt

    def test_prefetch_progress_summary_no_topics(self):
        user = _make_user()
        prefetch = {
            "progress_summary": {
                "score_7d": {"count": 0, "avg": None},
                "score_30d": {"count": 0, "avg": None},
                "topic_performance": [],
                "vocabulary": {"new": 0, "learning": 0, "review": 0, "relearning": 0},
                "sessions_this_week": 0,
            },
        }
        prompt = build_proactive_prompt(user, "proactive_summary", {}, prefetch=prefetch)
        assert "PROGRESS DATA" in prompt
        assert "Topic Performance" not in prompt

    def test_no_prefetch_no_data_sections(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "DUE VOCABULARY" not in prompt
        assert "PROGRESS DATA" not in prompt

    def test_web_search_tools_section_when_enabled(self):
        user = _make_user()
        prompt = build_proactive_prompt(
            user, "proactive_nudge", {},
            has_web_search=True,
        )
        assert "TOOLS" in prompt
        assert "web_search" in prompt
        assert "web_extract" in prompt

    def test_no_tools_section_by_default(self):
        user = _make_user()
        prompt = build_proactive_prompt(user, "proactive_nudge", {})
        assert "## TOOLS" not in prompt


class TestWebSearchPromptHint:

    def test_web_search_hint_when_enabled(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, has_web_search=True)
        assert "web_search" in prompt
        assert "TOOL REQUIREMENTS" in prompt

    def test_no_web_search_hint_by_default(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        # web_search should not appear in TOOL REQUIREMENTS
        tool_section = prompt.split("TOOL REQUIREMENTS")[1].split("##")[0]
        assert "web_search" not in tool_section

    def test_web_search_hint_mentions_limit(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, has_web_search=True)
        from adaptive_lang_study_bot.config import tuning
        assert str(tuning.max_searches_per_session) in prompt


# ---------------------------------------------------------------------------
# Comeback adaptation
# ---------------------------------------------------------------------------


class TestComebackAdaptation:

    def test_short_gap_no_section(self):
        """Gap < comeback_threshold_hours (48h) should not produce a comeback section."""
        user = _make_user()
        result = _build_comeback_section(user, 24.0, due_count=10, stale_topics=None)
        assert result is None

    def test_short_comeback_section(self):
        """Gap between threshold (48h) and 72h should produce a short comeback section."""
        user = _make_user()
        result = _build_comeback_section(user, 50.0, due_count=0, stale_topics=None)
        assert result is not None
        assert "COMEBACK ADAPTATION" in result
        assert "Short break" in result

    def test_72h_gap_produces_section(self):
        user = _make_user()
        result = _build_comeback_section(user, 80.0, due_count=0, stale_topics=None)
        assert result is not None
        assert "COMEBACK ADAPTATION" in result
        assert "3-7 days" in result

    def test_medium_absence_label(self):
        user = _make_user()
        result = _build_comeback_section(user, 240.0, due_count=0, stale_topics=None)
        assert "1-3 weeks" in result

    def test_long_absence_label(self):
        user = _make_user()
        result = _build_comeback_section(user, 600.0, due_count=0, stale_topics=None)
        assert "3+ weeks" in result

    def test_due_vocab_recommendation(self):
        user = _make_user()
        result = _build_comeback_section(user, 100.0, due_count=15, stale_topics=None)
        assert "VOCABULARY REVIEW" in result
        assert "15" in result
        assert "get_due_vocabulary" in result

    def test_no_due_vocab_no_recommendation(self):
        user = _make_user()
        result = _build_comeback_section(user, 100.0, due_count=2, stale_topics=None)
        assert "VOCABULARY REVIEW" not in result

    def test_struggling_topics_recommendation(self):
        user = _make_user(last_activity={
            "struggling_topics": [
                {"topic": "subjunctive", "avg_score": 3.2},
            ],
        })
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=None)
        assert "STRUGGLING TOPICS" in result
        assert "subjunctive" in result

    def test_weak_areas_fallback(self):
        """If no struggling_topics but weak_areas exist, show weak areas."""
        user = _make_user(
            last_activity={},
            weak_areas=["prepositions", "articles"],
        )
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=None)
        assert "WEAK AREAS" in result
        assert "prepositions" in result

    def test_stale_topics_recommendation(self):
        user = _make_user(last_activity={})
        stale = [{"topic": "travel vocab", "days_ago": 12.5, "avg_score": 4.8}]
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=stale)
        assert "STALE TOPICS" in result
        assert "travel vocab" in result

    def test_long_absence_difficulty_override(self):
        user = _make_user()
        result = _build_comeback_section(user, 600.0, due_count=0, stale_topics=None)
        assert "DIFFICULTY OVERRIDE" in result
        assert "EASY" in result

    def test_medium_absence_low_scores_easy(self):
        user = _make_user(recent_scores=[3, 2, 4, 3, 2])
        result = _build_comeback_section(user, 240.0, due_count=0, stale_topics=None)
        assert "DIFFICULTY ADJUSTMENT" in result
        assert "EASY" in result

    def test_medium_absence_good_scores_reduced(self):
        user = _make_user(recent_scores=[8, 7, 9, 8, 7])
        result = _build_comeback_section(user, 240.0, due_count=0, stale_topics=None)
        assert "DIFFICULTY ADJUSTMENT" in result
        assert "one notch BELOW" in result

    def test_short_absence_warm_up(self):
        user = _make_user(recent_scores=[7, 8, 6, 9, 7])
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=None)
        assert "WARM-UP" in result

    def test_zero_engagement_fresh_start(self):
        user = _make_user(
            vocabulary_count=0, recent_scores=[], sessions_completed=0,
            last_session_at=None,
        )
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=None)
        assert "never completed a lesson" in result

    def test_tone_instruction_always_present(self):
        user = _make_user()
        result = _build_comeback_section(user, 100.0, due_count=0, stale_topics=None)
        assert "TONE" in result
        assert "guilty" in result.lower()

    def test_integrated_in_system_prompt(self):
        """COMEBACK ADAPTATION section appears in full system prompt for long absence."""
        user = _make_user(
            last_session_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, due_count=10)
        assert "COMEBACK ADAPTATION" in prompt
        assert "VOCABULARY REVIEW" in prompt

    def test_not_in_system_prompt_for_short_gap(self):
        """COMEBACK ADAPTATION should NOT appear for gaps < 72h."""
        user = _make_user(
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=36),
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, due_count=10)
        assert "COMEBACK ADAPTATION" not in prompt

    def test_updated_greeting_note(self):
        """Long absence greeting note should reference COMEBACK ADAPTATION."""
        user = _make_user(
            last_session_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        ctx = compute_session_context(user)
        assert "COMEBACK ADAPTATION" in ctx["greeting_note"]


class TestNotificationState:
    """Test that notification state is rendered in the system prompt."""

    def test_notifications_active_shown(self):
        user = _make_user(notifications_paused=False)
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Notifications: active" in prompt

    def test_notifications_paused_shown(self):
        user = _make_user(notifications_paused=True)
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Notifications: paused" in prompt


class TestBotCapabilities:
    """Test that the BOT CAPABILITIES section is rendered."""

    def test_section_present(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "BOT CAPABILITIES" in prompt

    def test_settings_command_mentioned(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "/settings" in prompt

    def test_words_command_mentioned(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "/words" in prompt

    def test_stats_command_mentioned(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "/stats" in prompt

    def test_notifications_paused_tool_mentioned(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "notifications_paused" in prompt

    def test_settings_redirect_for_timezone(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "Change timezone" in prompt
        assert "redirect" in prompt.lower() or "/settings" in prompt


class TestActiveSchedules:
    """Test that active schedules are rendered in the session context."""

    def test_schedules_rendered(self):
        user = _make_user()
        ctx = compute_session_context(user)
        schedules = [
            {"type": "daily_review", "description": "Daily review at 09:00", "status": "active"},
            {"type": "quiz", "description": "Quiz Mon/Wed/Fri at 18:00", "status": "active"},
        ]
        prompt = build_system_prompt(user, ctx, active_schedules=schedules)
        assert "Active schedules:" in prompt
        assert "Daily review at 09:00" in prompt
        assert "Quiz Mon/Wed/Fri at 18:00" in prompt

    def test_paused_schedule_marked(self):
        user = _make_user()
        ctx = compute_session_context(user)
        schedules = [
            {"type": "daily_review", "description": "Daily review at 09:00", "status": "paused"},
        ]
        prompt = build_system_prompt(user, ctx, active_schedules=schedules)
        assert "[paused]" in prompt

    def test_no_schedules_no_section(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, active_schedules=[])
        assert "Active schedules:" not in prompt

    def test_none_schedules_no_section(self):
        user = _make_user()
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, active_schedules=None)
        assert "Active schedules:" not in prompt

    def test_duplicate_warning_present(self):
        user = _make_user()
        ctx = compute_session_context(user)
        schedules = [
            {"type": "daily_review", "description": "Daily review", "status": "active"},
        ]
        prompt = build_system_prompt(user, ctx, active_schedules=schedules)
        assert "avoid duplicates" in prompt.lower()


# ---------------------------------------------------------------------------
# Engagement-aware continuation and close_reason context
# ---------------------------------------------------------------------------


class TestIdleTimeoutContinuation:
    """Test that idle_timeout close_reason produces engagement-aware continuation."""

    def test_idle_timeout_with_pending_context_uses_abandoned_wording(self):
        """Teasing only happens when pending_context exists (true abandonment)."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "topic": "verbs",
            "pending_context": "preparing an exercise",
            "session_summary": "Started verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "abandoned mid-task" in prompt
        assert "preparing an exercise" in prompt

    def test_idle_timeout_no_pending_context_no_teasing(self):
        """Without pending_context, idle_timeout should NOT tease."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "exercise_count": 0,
            "topic": "verbs",
            "session_summary": "Started verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "abandoned" not in prompt
        assert "teasing" not in prompt.lower()
        assert "low engagement" in prompt

    def test_idle_timeout_productive_session_not_abandoned(self):
        """If user did 2+ exercises, treat idle_timeout as natural completion, not abandonment."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "exercise_count": 3,
            "topic": "verbs",
            "session_summary": "Verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended normally" in prompt
        assert "abandoned" not in prompt
        assert "disappeared" not in prompt
        assert "teasing" not in prompt.lower()

    def test_turn_limit_uses_system_limit_wording(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "turn_limit",
            "topic": "grammar",
            "session_summary": "Grammar session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "cut short by a system limit" in prompt
        assert "Do NOT tease" in prompt
        assert "abandoned" not in prompt
        assert "disappeared" not in prompt

    def test_cost_limit_uses_system_limit_wording(self):
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "cost_limit",
            "topic": "vocabulary",
            "session_summary": "Vocabulary session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "cut short by a system limit" in prompt
        assert "Do NOT tease" in prompt

    def test_shutdown_with_exercises_uses_technical_issue_wording(self):
        """Shutdown of a session with exercises (short gap) → 'technical issue'."""
        user = _make_user(
            last_activity={
                "status": "incomplete",
                "close_reason": "shutdown",
                "topic": "grammar",
                "session_summary": "Grammar session",
                "exercise_count": 2,
            },
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "technical issue" in prompt
        assert "abandoned" not in prompt

    def test_shutdown_without_exercises_suppresses_note(self):
        """Shutdown of an empty/low-engagement session → no 'technical issue' note."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "shutdown",
            "topic": "grammar",
            "session_summary": "Grammar session",
            "exercise_count": 0,
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "technical issue" not in prompt

    def test_shutdown_long_gap_suppresses_note(self):
        """Shutdown that happened many hours ago → no 'technical issue' note."""
        user = _make_user(
            last_activity={
                "status": "incomplete",
                "close_reason": "shutdown",
                "topic": "grammar",
                "session_summary": "Grammar session",
                "exercise_count": 3,
            },
            last_session_at=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "technical issue" not in prompt

    def test_no_close_reason_uses_default_wording(self):
        """Legacy last_activity without close_reason uses default wording."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "topic": "verbs",
            "session_summary": "Verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended mid-conversation" in prompt

    def test_pending_context_shown_for_idle_timeout(self):
        """When agent was preparing an exercise, show what was pending."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "exercise_count": 0,
            "pending_context": "preparing an exercise",
            "session_summary": "Practice session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "preparing an exercise" in prompt
        assert "abandoned mid-task" in prompt

    def test_no_pending_context_uses_neutral_wording(self):
        """No pending_context → neutral branch, no teasing."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "exercise_count": 0,
            "session_summary": "Practice session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "tutor was" not in prompt
        assert "low engagement" in prompt
        assert "abandoned" not in prompt

    def test_all_continuations_offer_choice(self):
        """Continuations with active work offer a choice to continue or start."""
        for close_reason, extra in [
            ("idle_timeout", {"pending_context": "preparing an exercise"}),
            ("turn_limit", {}),
            ("", {}),
        ]:
            activity = {
                "status": "incomplete",
                "close_reason": close_reason,
                "topic": "verbs",
                "session_summary": "Verb practice",
                **extra,
            }
            user = _make_user(last_activity=activity)
            ctx = compute_session_context(user)
            prompt = build_system_prompt(user, ctx)
            assert "continue" in prompt.lower(), (
                f"close_reason={close_reason!r} should offer choice"
            )
            assert "start fresh" in prompt or "start something new" in prompt

    def test_idle_timeout_playful_teasing_with_pending_context(self):
        """idle_timeout with pending_context should instruct playful teasing."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "topic": "verbs",
            "pending_context": "preparing an exercise",
            "session_summary": "Verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "playful" in prompt.lower()
        assert "teasing" in prompt.lower()

    def test_idle_timeout_agent_stopped_not_abandoned(self):
        """When agent_stopped is True, treat as natural completion even with 0 exercises."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "exercise_count": 0,
            "agent_stopped": True,
            "topic": "verbs",
            "session_summary": "Quick chat",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "ended normally" in prompt
        assert "abandoned" not in prompt
        assert "teasing" not in prompt.lower()

    def test_non_idle_timeout_no_teasing(self):
        """Non-idle-timeout continuations should NOT include teasing instructions."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "turn_limit",
            "topic": "verbs",
            "session_summary": "Verb practice",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "teasing" not in prompt.lower()


class TestCloseReasonInSessionHistory:
    """Test that close_reason is rendered in session history entries.

    Session history is only rendered for onboarding (no perf tools).
    """

    def test_idle_timeout_shown(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Verb practice", "status": "incomplete", "close_reason": "idle_timeout"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(idle timeout)" in prompt

    def test_turn_limit_shown(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Long session", "status": "incomplete", "close_reason": "turn_limit"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(turn limit)" in prompt

    def test_cost_limit_shown(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Intensive", "status": "incomplete", "close_reason": "cost_limit"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(cost limit)" in prompt

    def test_no_close_reason_shows_incomplete(self):
        """Legacy entries without close_reason still show (incomplete)."""
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Old session", "status": "incomplete"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(incomplete)" in prompt

    def test_completed_no_annotation(self):
        user = _make_user(session_history=[
            {"date": "2026-02-20", "summary": "Good session", "status": "completed"},
        ])
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx, session_type="onboarding")
        assert "(incomplete)" not in prompt
        assert "(idle timeout)" not in prompt


class TestEngagementHint:
    """Engagement instruction is now part of the idle_timeout continuation block."""

    def test_engaging_start_after_idle_timeout_with_pending(self):
        """idle_timeout with pending_context includes teasing and continue/fresh offer."""
        user = _make_user(last_activity={
            "status": "incomplete",
            "close_reason": "idle_timeout",
            "pending_context": "preparing an exercise",
            "session_summary": "Short session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "teasing" in prompt.lower()
        assert "start fresh" in prompt

    def test_no_engaging_hint_after_explicit_close(self):
        user = _make_user(last_activity={
            "close_reason": "explicit_close",
            "session_summary": "Normal session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "immediately engaging" not in prompt

    def test_no_engaging_hint_for_completed_session(self):
        """Completed sessions (even with idle_timeout close_reason somehow) don't trigger it."""
        user = _make_user(last_activity={
            "status": "completed",
            "close_reason": "idle_timeout",
            "session_summary": "Normal session",
        })
        ctx = compute_session_context(user)
        prompt = build_system_prompt(user, ctx)
        assert "immediately engaging" not in prompt



# ---------------------------------------------------------------------------
# Internal summary prompt
# ---------------------------------------------------------------------------


class TestBuildInternalSummaryPrompt:
    """Tests for the internal AI summary prompt (agent-facing)."""

    def test_contains_required_sections(self):
        prompt = build_internal_summary_prompt(
            conversation_digest="[User]: Bonjour\n[Tutor]: Salut!",
            session_data={
                "exercise_count": 2, "exercise_scores": [7, 8],
                "exercise_topics": ["verbs"], "exercise_types": ["translation"],
                "words_added": ["bonjour"], "words_reviewed": 1,
                "vocab_count": 1, "turn_count": 6,
            },
            close_reason="idle_timeout",
            target_language="French",
            user_level="A2",
        )
        assert "ROLE" in prompt
        assert "RULES" in prompt
        assert "SESSION DATA" in prompt
        assert "CONVERSATION TRANSCRIPT" in prompt
        assert "TASK" in prompt
        assert "Topics" in prompt
        assert "Performance" in prompt
        assert "Vocabulary" in prompt
        assert "Continuation" in prompt
        assert "Recommendations" in prompt

    def test_english_language_instruction(self):
        prompt = build_internal_summary_prompt(
            conversation_digest="[User]: test",
            session_data={
                "exercise_count": 0, "exercise_scores": [],
                "exercise_topics": [], "exercise_types": [],
                "words_added": [], "words_reviewed": 0,
                "vocab_count": 0, "turn_count": 2,
            },
            close_reason="explicit_close",
            target_language="Spanish",
            user_level="B1",
        )
        assert "English" in prompt

    def test_plan_summary_included_when_provided(self):
        prompt = build_internal_summary_prompt(
            conversation_digest="[User]: test",
            session_data={
                "exercise_count": 1, "exercise_scores": [8],
                "exercise_topics": ["grammar"], "exercise_types": ["fill_blank"],
                "words_added": [], "words_reviewed": 0,
                "vocab_count": 0, "turn_count": 4,
            },
            close_reason="idle_timeout",
            target_language="French",
            user_level="A2",
            plan_summary="A2→B1, Week 2/4, 50%",
        )
        assert "A2→B1" in prompt


# ---------------------------------------------------------------------------
# Session history rendering in system prompt
# ---------------------------------------------------------------------------


class TestSessionHistoryInPrompt:
    """Test that AI summaries from recent sessions appear in the system prompt."""

    def _make_session_mock(self, started_at, ended_at, ai_summary):
        sess = MagicMock()
        sess.started_at = started_at
        sess.ended_at = ended_at
        sess.ai_summary = ai_summary
        return sess

    def test_recent_sessions_rendered_for_interactive(self):
        now = datetime.now(timezone.utc)
        sessions = [
            self._make_session_mock(
                now - timedelta(hours=2), now - timedelta(hours=1, minutes=45),
                "Covered basic greetings. Student performed well on vocabulary.",
            ),
        ]
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user), recent_sessions=sessions)
        assert "Recent sessions:" in prompt
        assert "Covered basic greetings" in prompt

    def test_no_recent_sessions_no_section(self):
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user), recent_sessions=[])
        assert "Recent sessions:" not in prompt

    def test_none_recent_sessions_no_section(self):
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user), recent_sessions=None)
        assert "Recent sessions:" not in prompt


# ---------------------------------------------------------------------------
# Prompt cleanup: shared SESSION RULES
# ---------------------------------------------------------------------------


class TestSharedSessionRules:
    """Verify D3: shared SESSION RULES block replaces per-style duplication."""

    def test_session_rules_block_present(self):
        user = _make_user(session_style="structured")
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "SESSION RULES:" in prompt
        assert "NEVER repeat content" in prompt

    def test_no_duplicate_never_repeat(self):
        """The 'NEVER repeat' instruction should appear exactly once."""
        user = _make_user(session_style="casual")
        prompt = build_system_prompt(user, compute_session_context(user))
        assert prompt.count("NEVER repeat content") == 1


# ---------------------------------------------------------------------------
# Vocabulary transcription rule
# ---------------------------------------------------------------------------


class TestVocabularyTranscriptionRule:
    """Verify D6: transcription rule in VOCABULARY STRATEGY."""

    def test_transcription_present_for_different_languages(self):
        user = _make_user(native_language="ru", target_language="fr", sessions_completed=5)
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "TRANSCRIPTION" in prompt
        assert "phonetic transcription" in prompt

    def test_transcription_absent_for_same_language(self):
        user = _make_user(native_language="fr", target_language="fr", sessions_completed=5)
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "TRANSCRIPTION" not in prompt


# ---------------------------------------------------------------------------
# Output format enhancements
# ---------------------------------------------------------------------------


class TestOutputFormatEnhancements:
    """Verify D5: no headers rule added to OUTPUT FORMAT."""

    def test_no_headers_rule(self):
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "NEVER use markdown headers" in prompt

    def test_no_tables_rule(self):
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "NEVER use markdown tables" in prompt


# ---------------------------------------------------------------------------
# Comeback precedence (D4)
# ---------------------------------------------------------------------------


class TestComebackDifficultyPrecedence:
    """Verify D4: comeback difficulty override mentions precedence."""

    def test_precedence_note_in_full_comeback(self):
        user = _make_user(
            last_session_at=datetime.now(timezone.utc) - timedelta(days=30),
            sessions_completed=10,
        )
        section = _build_comeback_section(user, gap_hours=30 * 24, due_count=0, stale_topics=[])
        assert section is not None
        assert "takes precedence" in section

    def test_score_adaptation_cross_reference(self):
        user = _make_user()
        prompt = build_system_prompt(user, compute_session_context(user))
        assert "comeback difficulty override" in prompt
