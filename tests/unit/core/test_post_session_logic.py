"""Unit tests for post-session pipeline logic.

Tests the deleted-user guard, profile validation, session status
classification, and milestone detection.
These are pure logic tests — no DB, no LLM.
"""

from adaptive_lang_study_bot.enums import CloseReason, Difficulty
from adaptive_lang_study_bot.config import CEFR_LEVELS
from adaptive_lang_study_bot.pipeline.post_session import _FORCED_CLOSE_REASONS


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

    def test_invalid_level(self):
        assert "X9" not in CEFR_LEVELS
        assert "A0" not in CEFR_LEVELS
        assert "" not in CEFR_LEVELS


class TestSessionStatus:
    """Test session status determination based on close reason."""

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
