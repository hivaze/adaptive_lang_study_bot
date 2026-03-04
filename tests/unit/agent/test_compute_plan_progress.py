"""Unit tests for compute_plan_progress — learning plan progress derivation."""

import pytest

from adaptive_lang_study_bot.agent.tools import compute_plan_progress
from adaptive_lang_study_bot.config import tuning

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


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


class TestConsolidationPhase:
    """Tests for consolidation phase behavior in compute_plan_progress."""

    def _make_plan_with_consolidation(self, regular_topics, consolidation_topics):
        """Build plan_data with regular phases + a consolidation phase."""
        phases = [
            {
                "week": 1,
                "focus": "Phase 1",
                "start_date": "2026-03-01",
                "end_date": "2026-03-07",
                "topics": regular_topics,
            },
            {
                "week": 2,
                "focus": f"Consolidation — strengthen for B1",
                "start_date": "2026-03-08",
                "end_date": "2026-03-14",
                "topics": consolidation_topics,
                "consolidation": True,
            },
        ]
        return {
            "phases": phases,
            "consolidation_added": True,
            "consolidation_added_at": "2026-03-08",
        }

    def test_consolidation_uses_level_up_threshold(self):
        """Consolidation topics need level_up_avg (8.5) to be completed, not 7.0."""
        plan_data = self._make_plan_with_consolidation(
            regular_topics=["Verbs"],
            consolidation_topics=["Verbs"],
        )
        # Score of 7.5 — enough for regular (>= 7.0) but NOT for consolidation (>= 8.5)
        topic_stats = {
            "Verbs": {"count": 5, "avg_score": 7.5, "last_practiced": "2026-03-10"},
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        # Regular phase: completed (7.5 >= 7.0)
        assert progress["phases"][0]["topics"][0]["status"] == "completed"
        # Consolidation phase: in_progress (7.5 < 8.5)
        assert progress["phases"][1]["topics"][0]["status"] == "in_progress"
        assert progress["phases"][1].get("consolidation") is True

    def test_consolidation_completed_at_high_score(self):
        """Consolidation topics complete when avg >= level_up_avg."""
        plan_data = self._make_plan_with_consolidation(
            regular_topics=["Verbs"],
            consolidation_topics=["Verbs"],
        )
        topic_stats = {
            "Verbs": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-10"},
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        assert progress["phases"][1]["topics"][0]["status"] == "completed"
        assert progress["progress_pct"] == 100

    def test_consolidation_flag_propagated_to_result(self):
        """Consolidation flag should appear in phase results."""
        plan_data = self._make_plan_with_consolidation(
            regular_topics=["A"],
            consolidation_topics=["A"],
        )
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), {},
        )
        assert progress["phases"][0].get("consolidation") is None
        assert progress["phases"][1]["consolidation"] is True

    def test_regular_phase_not_affected_by_consolidation_threshold(self):
        """Regular phases should still use plan_topic_mastery_score (7.0)."""
        plan_data = self._make_plan_with_consolidation(
            regular_topics=["Nouns"],
            consolidation_topics=["Verbs"],
        )
        topic_stats = {
            "Nouns": {"count": 3, "avg_score": 7.0, "last_practiced": "2026-03-05"},
            "Verbs": {"count": 3, "avg_score": 7.0, "last_practiced": "2026-03-10"},
        }
        progress = compute_plan_progress(
            plan_data, 2, date(2026, 3, 1), topic_stats,
        )
        # Regular: 7.0 >= 7.0 → completed
        assert progress["phases"][0]["topics"][0]["status"] == "completed"
        # Consolidation: 7.0 < 8.5 → in_progress
        assert progress["phases"][1]["topics"][0]["status"] == "in_progress"


class TestMaybeAddConsolidationPhase:
    """Tests for _maybe_add_consolidation_phase guard logic."""

    @pytest.fixture()
    def _mock_plan(self):
        plan = MagicMock()
        plan.id = "plan-123"
        plan.current_level = "A2"
        plan.target_level = "B1"
        plan.start_date = date(2026, 3, 1)
        plan.target_end_date = date(2026, 4, 12)
        plan.total_weeks = 4
        plan.plan_data = {
            "phases": [
                {"week": 1, "focus": "P1", "topics": ["A", "B"]},
                {"week": 2, "focus": "P2", "topics": ["C"]},
            ],
        }
        return plan

    @pytest.fixture()
    def _full_progress(self):
        return {
            "progress_pct": 100,
            "completed_topics": 3,
            "total_topics": 3,
            "phases": [
                {"status": "completed", "topics": [
                    {"name": "A", "status": "completed", "exercises": 5},
                    {"name": "B", "status": "completed", "exercises": 4},
                ]},
                {"status": "completed", "topics": [
                    {"name": "C", "status": "completed", "exercises": 3},
                ]},
            ],
        }

    @pytest.fixture()
    def _topic_stats(self):
        return {
            "A": {"count": 5, "avg_score": 7.5, "last_practiced": "2026-03-10"},
            "B": {"count": 4, "avg_score": 8.0, "last_practiced": "2026-03-10"},
            "C": {"count": 3, "avg_score": 7.2, "last_practiced": "2026-03-10"},
        }

    @pytest.mark.asyncio
    async def test_skips_when_already_added(self, _mock_plan, _full_progress, _topic_stats):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        _mock_plan.plan_data["consolidation_added"] = True
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, _topic_stats, "A2",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_not_100_percent(self, _mock_plan, _full_progress, _topic_stats):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        _full_progress["progress_pct"] = 80
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, _topic_stats, "A2",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_level_at_target(self, _mock_plan, _full_progress, _topic_stats):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, _topic_stats, "B1",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_at_max_weeks(self, _mock_plan, _full_progress, _topic_stats):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        _mock_plan.total_weeks = tuning.plan_max_weeks
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, _topic_stats, "A2",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_all_topics_above_threshold(self, _mock_plan, _full_progress):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        topic_stats = {
            "A": {"count": 5, "avg_score": 9.0, "last_practiced": "2026-03-10"},
            "B": {"count": 4, "avg_score": 9.5, "last_practiced": "2026-03-10"},
            "C": {"count": 3, "avg_score": 8.5, "last_practiced": "2026-03-10"},
        }
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, topic_stats, "A2",
        )
        assert result is False

    @pytest.mark.asyncio
    @patch("adaptive_lang_study_bot.agent.tools.LearningPlanRepo.update_fields", new_callable=AsyncMock)
    async def test_adds_consolidation_phase(self, mock_update, _mock_plan, _full_progress, _topic_stats):
        from adaptive_lang_study_bot.agent.tools import _maybe_add_consolidation_phase
        db = AsyncMock()
        result = await _maybe_add_consolidation_phase(
            db, _mock_plan, _full_progress, _topic_stats, "A2",
        )
        assert result is True
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args[1]
        new_plan_data = call_kwargs["plan_data"]
        assert new_plan_data["consolidation_added"] is True
        assert "consolidation_added_at" in new_plan_data
        # Consolidation phase should be last
        consol_phase = new_plan_data["phases"][-1]
        assert consol_phase["consolidation"] is True
        # Should contain the weakest topics (C at 7.2, A at 7.5, B at 8.0)
        assert "C" in consol_phase["topics"]
        assert "A" in consol_phase["topics"]
        assert call_kwargs["total_weeks"] == _mock_plan.total_weeks + 1
