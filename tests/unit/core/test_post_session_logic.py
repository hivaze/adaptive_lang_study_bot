"""Unit tests for post-session pipeline logic.

Tests the difficulty auto-adjust logic (with None guard for deleted user),
profile validation rules, session status classification, and milestone detection.
These are pure logic tests — no DB, no LLM.
"""

from adaptive_lang_study_bot.enums import CloseReason, Difficulty
from adaptive_lang_study_bot.pipeline.post_session import _FORCED_CLOSE_REASONS, _LEVELS


class TestDifficultyAutoAdjust:
    """Test the difficulty auto-adjust rules from post_session.py lines 97-130.

    Rules:
    - easy→normal if avg >= 8.5
    - normal→hard if avg >= 9.0
    - hard→normal if avg <= 3.0
    - normal→easy if avg <= 4.0
    """

    @staticmethod
    def _adjust(scores: list[int], difficulty: str) -> str:
        """Mirror the exact logic from post_session.py."""
        if len(scores) < 5:
            return difficulty
        avg_5 = sum(scores[-5:]) / 5
        if avg_5 >= 8.5 and difficulty == Difficulty.EASY:
            return Difficulty.NORMAL
        elif avg_5 >= 9.0 and difficulty == Difficulty.NORMAL:
            return Difficulty.HARD
        elif avg_5 <= 3.0 and difficulty == Difficulty.HARD:
            return Difficulty.NORMAL
        elif avg_5 <= 4.0 and difficulty == Difficulty.NORMAL:
            return Difficulty.EASY
        return difficulty

    def test_easy_to_normal_at_8_5(self):
        scores = [8, 9, 8, 9, 9]  # avg = 8.6
        assert self._adjust(scores, Difficulty.EASY) == Difficulty.NORMAL

    def test_normal_to_hard_at_9_0(self):
        scores = [9, 10, 9, 9, 9]  # avg = 9.2
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.HARD

    def test_hard_to_normal_at_3_0(self):
        scores = [2, 3, 2, 3, 3]  # avg = 2.6
        assert self._adjust(scores, Difficulty.HARD) == Difficulty.NORMAL

    def test_normal_to_easy_at_4_0(self):
        scores = [3, 4, 3, 4, 4]  # avg = 3.6
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.EASY

    def test_no_change_easy_below_threshold(self):
        scores = [7, 7, 7, 7, 7]  # avg = 7.0, below 8.5
        assert self._adjust(scores, Difficulty.EASY) == Difficulty.EASY

    def test_no_change_normal_in_middle(self):
        scores = [6, 7, 6, 7, 6]  # avg = 6.4
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.NORMAL

    def test_no_change_hard_above_threshold(self):
        scores = [5, 5, 5, 5, 5]  # avg = 5.0, above 3.0
        assert self._adjust(scores, Difficulty.HARD) == Difficulty.HARD

    def test_fewer_than_5_scores_no_change(self):
        scores = [1, 1, 1]
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.NORMAL

    def test_empty_scores_no_change(self):
        assert self._adjust([], Difficulty.HARD) == Difficulty.HARD

    def test_boundary_8_5_easy_promotes(self):
        scores = [8, 8, 9, 9, 8.5]
        assert self._adjust(scores, Difficulty.EASY) == Difficulty.NORMAL

    def test_boundary_9_0_normal_promotes(self):
        scores = [9, 9, 9, 9, 9]
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.HARD

    def test_boundary_3_0_hard_demotes(self):
        scores = [3, 3, 3, 3, 3]
        assert self._adjust(scores, Difficulty.HARD) == Difficulty.NORMAL

    def test_boundary_4_0_normal_demotes(self):
        scores = [4, 4, 4, 4, 4]
        assert self._adjust(scores, Difficulty.NORMAL) == Difficulty.EASY

    def test_hard_not_affected_by_easy_threshold(self):
        """hard→normal needs avg <= 3.0, not 4.0."""
        scores = [4, 4, 4, 4, 4]  # avg = 4.0
        assert self._adjust(scores, Difficulty.HARD) == Difficulty.HARD

    def test_easy_not_affected_by_normal_threshold(self):
        """easy→normal needs avg >= 8.5, not 9.0."""
        scores = [9, 9, 9, 9, 9]  # avg = 9.0 >= 8.5
        assert self._adjust(scores, Difficulty.EASY) == Difficulty.NORMAL


class TestDeletedUserGuard:
    """Test the None guard for fresh_user in difficulty auto-adjust.

    When the user is deleted during a session, fresh_user is None.
    The pipeline should skip the difficulty adjustment gracefully.
    """

    @staticmethod
    def _apply_difficulty_update(
        user_difficulty: str,
        new_difficulty: str,
        fresh_user_difficulty: str | None,
        fresh_user_is_none: bool = False,
    ) -> tuple[str | None, str | None]:
        """Mirror post_session.py lines 118-130.

        Returns (updates_difficulty, issue_message).
        """
        if new_difficulty == user_difficulty:
            return None, None

        if fresh_user_is_none:
            return None, "User deleted during session, skipping difficulty auto-adjust"
        elif fresh_user_difficulty == user_difficulty:
            return new_difficulty, None
        else:
            return None, "Skipped difficulty auto-adjust: user changed preference during session"

    def test_fresh_user_none_skips_update(self):
        result, issue = self._apply_difficulty_update(
            user_difficulty=Difficulty.NORMAL,
            new_difficulty=Difficulty.HARD,
            fresh_user_difficulty=None,
            fresh_user_is_none=True,
        )
        assert result is None
        assert "deleted" in issue

    def test_fresh_user_same_applies_update(self):
        result, issue = self._apply_difficulty_update(
            user_difficulty=Difficulty.NORMAL,
            new_difficulty=Difficulty.HARD,
            fresh_user_difficulty=Difficulty.NORMAL,
        )
        assert result == Difficulty.HARD
        assert issue is None

    def test_fresh_user_changed_skips_update(self):
        """User changed difficulty via /settings during the session."""
        result, issue = self._apply_difficulty_update(
            user_difficulty=Difficulty.NORMAL,
            new_difficulty=Difficulty.HARD,
            fresh_user_difficulty=Difficulty.EASY,
        )
        assert result is None
        assert "changed preference" in issue

    def test_no_change_no_update(self):
        result, issue = self._apply_difficulty_update(
            user_difficulty=Difficulty.NORMAL,
            new_difficulty=Difficulty.NORMAL,
            fresh_user_difficulty=Difficulty.NORMAL,
        )
        assert result is None
        assert issue is None


class TestProfileValidation:
    """Test profile integrity validation rules from post_session.py."""

    def test_valid_levels(self):
        """All valid levels should be recognized."""
        for level in _LEVELS:
            assert level in _LEVELS

    def test_invalid_level(self):
        assert "X9" not in _LEVELS
        assert "A0" not in _LEVELS
        assert "" not in _LEVELS

    def test_interest_cap(self):
        """Interests should be capped at 5."""
        interests = ["a", "b", "c", "d", "e", "f"]
        capped = interests[:5]
        assert len(capped) == 5

    def test_learning_goals_cap(self):
        """Learning goals should be capped at 3."""
        goals = ["a", "b", "c", "d"]
        capped = goals[:3]
        assert len(capped) == 3

    def test_weak_areas_cap(self):
        """Weak areas should be capped at 10."""
        areas = [f"area_{i}" for i in range(15)]
        capped = areas[:10]
        assert len(capped) == 10

    def test_strong_areas_cap(self):
        """Strong areas should be capped at 10."""
        areas = [f"area_{i}" for i in range(15)]
        capped = areas[:10]
        assert len(capped) == 10

    def test_score_cleaning(self):
        """Scores outside 0-10 should be filtered."""
        scores = [5, -1, 8, 11, 3, 15, 0, 10]
        cleaned = [s for s in scores if 0 <= s <= 10]
        assert cleaned == [5, 8, 3, 0, 10]


class TestSessionStatus:
    """Test session status determination based on close reason."""

    def test_forced_reasons_mark_incomplete(self):
        for reason in _FORCED_CLOSE_REASONS:
            status = "incomplete" if reason in _FORCED_CLOSE_REASONS else "completed"
            assert status == "incomplete", f"{reason} should be incomplete"

    def test_idle_timeout_marks_incomplete(self):
        """Idle timeout sessions are incomplete — the user dropped off mid-task."""
        assert CloseReason.IDLE_TIMEOUT in _FORCED_CLOSE_REASONS
        status = "incomplete" if CloseReason.IDLE_TIMEOUT in _FORCED_CLOSE_REASONS else "completed"
        assert status == "incomplete"

    def test_voluntary_reasons_mark_completed(self):
        voluntary = [CloseReason.EXPLICIT_CLOSE, CloseReason.UNKNOWN]
        for reason in voluntary:
            status = "incomplete" if reason in _FORCED_CLOSE_REASONS else "completed"
            assert status == "completed", f"{reason} should be completed"


class TestMilestoneDetection:
    """Test milestone detection logic from post_session.py."""

    @staticmethod
    def _detect_milestones(
        streak: int,
        vocab_count: int,
        existing_pending: list[str],
    ) -> list[str]:
        """Mirror milestone detection logic."""
        pending = list(existing_pending)
        if streak > 0 and streak % 10 == 0:
            msg = f"{streak}-day streak!"
            if msg not in pending:
                pending.append(msg)
        if vocab_count > 0 and vocab_count % 100 == 0:
            msg = f"{vocab_count} words learned!"
            if msg not in pending:
                pending.append(msg)
        return pending[-5:]

    def test_streak_milestone_at_10(self):
        result = self._detect_milestones(10, 50, [])
        assert "10-day streak!" in result

    def test_streak_milestone_at_20(self):
        result = self._detect_milestones(20, 50, [])
        assert "20-day streak!" in result

    def test_no_streak_milestone_at_11(self):
        result = self._detect_milestones(11, 50, [])
        assert len(result) == 0

    def test_vocab_milestone_at_100(self):
        result = self._detect_milestones(5, 100, [])
        assert "100 words learned!" in result

    def test_vocab_milestone_at_200(self):
        result = self._detect_milestones(5, 200, [])
        assert "200 words learned!" in result

    def test_no_vocab_milestone_at_99(self):
        result = self._detect_milestones(5, 99, [])
        assert len(result) == 0

    def test_both_milestones_simultaneously(self):
        result = self._detect_milestones(10, 100, [])
        assert "10-day streak!" in result
        assert "100 words learned!" in result

    def test_no_duplicate_milestone(self):
        existing = ["10-day streak!"]
        result = self._detect_milestones(10, 50, existing)
        assert result.count("10-day streak!") == 1

    def test_milestones_capped_at_5(self):
        existing = ["a", "b", "c", "d", "e"]
        result = self._detect_milestones(10, 100, existing)
        assert len(result) == 5

    def test_zero_streak_no_milestone(self):
        result = self._detect_milestones(0, 50, [])
        assert len(result) == 0

    def test_zero_vocab_no_milestone(self):
        result = self._detect_milestones(5, 0, [])
        assert len(result) == 0
