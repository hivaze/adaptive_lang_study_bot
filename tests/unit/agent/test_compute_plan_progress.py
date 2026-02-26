"""Unit tests for compute_plan_progress — learning plan progress derivation."""

import pytest

from adaptive_lang_study_bot.agent.tools import compute_plan_progress
from adaptive_lang_study_bot.config import tuning

from datetime import date, timedelta


def _make_plan_data(*, topics_per_phase=None):
    """Build minimal plan_data with given topics per phase."""
    if topics_per_phase is None:
        topics_per_phase = [
            ["Past tense review", "Conditional mood"],
            ["Subjunctive introduction", "Complex sentences"],
        ]
    phases = []
    start = date(2026, 3, 1)
    for i, topics in enumerate(topics_per_phase):
        week_start = start + timedelta(weeks=i)
        phases.append({
            "week": i + 1,
            "focus": f"Phase {i + 1}",
            "start_date": week_start.isoformat(),
            "end_date": (week_start + timedelta(days=6)).isoformat(),
            "topics": topics,
            "vocabulary_target": 10,
        })
    return {"phases": phases}


class TestProgressDerivation:

    def test_empty_plan_returns_zero(self):
        progress = compute_plan_progress(
            {"phases": []}, 0, date(2026, 3, 1), {},
        )
        assert progress["progress_pct"] == 0
        assert progress["completed_topics"] == 0
        assert progress["total_topics"] == 0

    def test_no_exercises_all_pending(self):
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), {},
        )
        assert progress["progress_pct"] == 0
        assert progress["completed_topics"] == 0
        assert progress["total_topics"] == 4
        for phase in progress["phases"]:
            assert phase["status"] == "pending"
            for topic in phase["topics"]:
                assert topic["status"] == "pending"

    def test_topic_completed_when_mastered(self):
        topic_stats = {
            "Past tense review": {
                "count": tuning.plan_topic_min_exercises,
                "avg_score": tuning.plan_topic_mastery_score,
                "last_practiced": "2026-03-05",
            },
        }
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        assert progress["completed_topics"] == 1
        assert progress["total_topics"] == 4
        assert progress["progress_pct"] == 25

        # Verify specific topic
        phase_1_topics = progress["phases"][0]["topics"]
        past_tense = next(t for t in phase_1_topics if t["name"] == "Past tense review")
        assert past_tense["status"] == "completed"

    def test_topic_in_progress_not_enough_exercises(self):
        topic_stats = {
            "Past tense review": {
                "count": 1,
                "avg_score": 8.0,
                "last_practiced": "2026-03-05",
            },
        }
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        past_tense = progress["phases"][0]["topics"][0]
        assert past_tense["status"] == "in_progress"

    def test_topic_in_progress_low_score(self):
        topic_stats = {
            "Past tense review": {
                "count": tuning.plan_topic_min_exercises,
                "avg_score": tuning.plan_topic_mastery_score - 1,
                "last_practiced": "2026-03-05",
            },
        }
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        past_tense = progress["phases"][0]["topics"][0]
        assert past_tense["status"] == "in_progress"

    def test_all_topics_completed(self):
        topics_per_phase = [["A", "B"], ["C"]]
        plan_data = _make_plan_data(topics_per_phase=topics_per_phase)
        topic_stats = {
            t: {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-10"}
            for t in ["A", "B", "C"]
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        assert progress["progress_pct"] == 100
        assert progress["completed_topics"] == 3
        for phase in progress["phases"]:
            assert phase["status"] == "completed"

    def test_phase_status_in_progress(self):
        """Phase is in_progress if some (not all) topics are started."""
        plan_data = _make_plan_data()
        topic_stats = {
            "Past tense review": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-05"},
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        assert progress["phases"][0]["status"] == "in_progress"
        assert progress["phases"][1]["status"] == "pending"

    def test_phase_completed_when_all_topics_done(self):
        plan_data = _make_plan_data()
        topic_stats = {
            "Past tense review": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-05"},
            "Conditional mood": {"count": 4, "avg_score": 8.0, "last_practiced": "2026-03-05"},
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        assert progress["phases"][0]["status"] == "completed"
        assert progress["phases"][1]["status"] == "pending"

    def test_progress_pct_rounds(self):
        """1 out of 3 = 33% (rounded)."""
        topics_per_phase = [["A", "B", "C"]]
        plan_data = _make_plan_data(topics_per_phase=topics_per_phase)
        topic_stats = {
            "A": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-05"},
        }
        progress = compute_plan_progress(
            plan_data, 1, date(2026, 3, 1), topic_stats,
        )
        assert progress["progress_pct"] == 33

    def test_topic_detail_includes_exercise_count(self):
        topic_stats = {
            "Past tense review": {"count": 3, "avg_score": 7.5, "last_practiced": "2026-03-05"},
        }
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        topic = progress["phases"][0]["topics"][0]
        assert topic["exercises"] == 3
        assert topic["avg_score"] == 7.5

    def test_no_in_strong_or_in_weak_keys(self):
        """Progress output should not contain in_strong or in_weak keys."""
        topic_stats = {
            "Past tense review": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-05"},
        }
        plan_data = _make_plan_data()
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        for phase in progress["phases"]:
            for topic in phase["topics"]:
                assert "in_strong" not in topic
                assert "in_weak" not in topic
