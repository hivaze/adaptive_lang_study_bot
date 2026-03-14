"""Unit tests for score normalization consistency.

Ensures that raw exercise scores (stored as score/max_score) are correctly
normalized to the 0-10 scale before comparison against tuning thresholds.
Pure logic tests — no DB, no LLM.
"""

from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.utils import score_label


class TestScoreLabel:
    """score_label() operates on 0-10 normalized scale."""

    def test_perfect_score(self):
        assert score_label(10) == "excellent"

    def test_very_good_score(self):
        assert score_label(8) == "very good"

    def test_good_score(self):
        assert score_label(7) == "good"

    def test_needs_work(self):
        assert score_label(5) == "needs work"

    def test_poor_score(self):
        assert score_label(2) == "poor"

    def test_zero_score(self):
        assert score_label(0) == "poor"

    def test_none_returns_unknown(self):
        assert score_label(None) == "unknown"

    def test_boundary_3_is_poor(self):
        assert score_label(3) == "poor"

    def test_boundary_5_is_needs_work(self):
        assert score_label(5) == "needs work"

    def test_boundary_7_is_good(self):
        assert score_label(7) == "good"

    def test_boundary_9_is_very_good(self):
        assert score_label(9) == "very good"


class TestTuningThresholdsScale:
    """All tuning score thresholds must be on the 0-10 normalized scale."""

    def test_weak_area_score_in_range(self):
        assert 0 <= tuning.weak_area_score <= 10

    def test_strong_area_score_in_range(self):
        assert 0 <= tuning.strong_area_score <= 10

    def test_strong_above_weak(self):
        assert tuning.strong_area_score > tuning.weak_area_score

    def test_plan_mastery_in_range(self):
        assert 0 < tuning.plan_topic_mastery_score <= 10

    def test_consolidation_mastery_in_range(self):
        assert 0 < tuning.consolidation_mastery_score <= 10

    def test_consolidation_above_regular_mastery(self):
        assert tuning.consolidation_mastery_score > tuning.plan_topic_mastery_score

    def test_hook_struggling_in_range(self):
        assert 0 <= tuning.hook_struggling_threshold <= 10

    def test_hook_excelling_in_range(self):
        assert 0 <= tuning.hook_excelling_threshold <= 10

    def test_hook_excelling_above_struggling(self):
        assert tuning.hook_excelling_threshold > tuning.hook_struggling_threshold

    def test_hook_single_struggling_in_range(self):
        assert 0 <= tuning.hook_single_struggling_threshold <= 10

    def test_hook_single_excellent_in_range(self):
        assert 0 <= tuning.hook_single_excellent_threshold <= 10

    def test_hook_single_excellent_above_struggling(self):
        assert tuning.hook_single_excellent_threshold > tuning.hook_single_struggling_threshold


class TestNormalizationFormula:
    """Verify the normalization formula: round(score * 10 / max_score)."""

    @staticmethod
    def _normalize(score: int, max_score: int) -> int:
        return round(score * 10 / max_score) if max_score else 0

    def test_5_out_of_5_is_10(self):
        assert self._normalize(5, 5) == 10

    def test_3_out_of_5_is_6(self):
        assert self._normalize(3, 5) == 6

    def test_1_out_of_5_is_2(self):
        assert self._normalize(1, 5) == 2

    def test_4_out_of_4_is_10(self):
        assert self._normalize(4, 4) == 10

    def test_3_out_of_4_is_8(self):
        assert self._normalize(3, 4) == 8

    def test_1_out_of_1_is_10(self):
        assert self._normalize(1, 1) == 10

    def test_0_out_of_5_is_0(self):
        assert self._normalize(0, 5) == 0

    def test_7_out_of_10_is_7(self):
        assert self._normalize(7, 10) == 7

    def test_10_out_of_10_is_10(self):
        assert self._normalize(10, 10) == 10

    def test_max_score_zero_returns_zero(self):
        """Edge case: max_score=0 should not crash (division by zero guard)."""
        assert self._normalize(5, 0) == 0

    def test_normalized_score_within_weak_threshold(self):
        """3/5 = 6 normalized, which is above weak_area_score (5) — NOT weak."""
        norm = self._normalize(3, 5)
        assert norm > tuning.weak_area_score

    def test_raw_score_misleading_without_normalization(self):
        """Without normalization, 3/5 raw score would be classified as weak (<= 5).
        This was the original bug — raw scores compared against 0-10 thresholds."""
        raw = 3  # 3 out of 5
        norm = self._normalize(3, 5)  # = 6
        # Raw would be classified as weak (3 <= 5), but normalized is not (6 > 5)
        assert raw <= tuning.weak_area_score
        assert norm > tuning.weak_area_score

    def test_1_out_of_4_is_weak(self):
        """1/4 = 2.5 → 3 normalized, which IS weak (<= 5)."""
        norm = self._normalize(1, 4)
        assert norm <= tuning.weak_area_score

    def test_4_out_of_5_is_strong(self):
        """4/5 = 8 normalized, which IS strong (>= 7)."""
        norm = self._normalize(4, 5)
        assert norm >= tuning.strong_area_score

    def test_plan_mastery_reachable(self):
        """With normalization, plan mastery (7.0) is achievable with 4/5 scores."""
        norm = self._normalize(4, 5)
        assert norm >= tuning.plan_topic_mastery_score


class TestPostSessionNormalization:
    """Test that post-session activity logging normalizes scores correctly."""

    @staticmethod
    def _compute_struggling(exercises: list[tuple[int, int]]) -> list[dict]:
        """Mirror post_session.py struggling topic detection with normalization.

        exercises: list of (score, max_score) tuples for a single topic.
        """
        normalized = [
            round(score * 10 / max_score) if max_score else 0
            for score, max_score in exercises
        ]
        avg = sum(normalized) / len(normalized) if normalized else 0
        if avg <= 5:
            return [{"topic": "test", "avg_score": round(avg, 1)}]
        return []

    def test_good_raw_scores_not_struggling(self):
        """3/4 and 4/5 are good scores (7.5 and 8.0 normalized) — not struggling."""
        result = self._compute_struggling([(3, 4), (4, 5)])
        assert result == []

    def test_poor_scores_are_struggling(self):
        """1/5 and 1/4 are poor scores (2.0 and 2.5 normalized) — struggling."""
        result = self._compute_struggling([(1, 5), (1, 4)])
        assert len(result) == 1
        assert result[0]["avg_score"] <= 5

    def test_mixed_scores_not_struggling(self):
        """Mix of 2/4 and 4/5: normalized 5.0 and 8.0, avg 6.5 — not struggling."""
        result = self._compute_struggling([(2, 4), (4, 5)])
        assert result == []

    def test_max_score_1_perfect_not_struggling(self):
        """1/1 = 10 normalized — definitely not struggling."""
        result = self._compute_struggling([(1, 1)])
        assert result == []
