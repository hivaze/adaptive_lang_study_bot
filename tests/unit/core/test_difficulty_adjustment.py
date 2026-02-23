"""Tests for difficulty auto-adjustment logic in post_session.py.

Uses the actual tuning constants from config.py so the tests stay
in sync with production thresholds.

Production transitions (post_session.py):
  easy   → normal : avg >= difficulty_up_easy_normal   (7.5)
  normal → hard   : avg >= difficulty_up_normal_hard   (8.0)
  hard   → normal : avg <= difficulty_down_hard_normal (4.5)
  normal → easy   : avg <= difficulty_down_normal_easy (4.5)
"""

from adaptive_lang_study_bot.config import tuning


def _auto_adjust(recent_scores: list[float], current_difficulty: str) -> str:
    """Mirror the post-session pipeline difficulty adjustment logic.

    Must stay in sync with the Step 3 block in run_post_session().
    """
    window = tuning.difficulty_recent_window
    if len(recent_scores) < window:
        return current_difficulty

    avg = sum(recent_scores[-window:]) / window

    if avg >= tuning.difficulty_up_easy_normal and current_difficulty == "easy":
        return "normal"
    elif avg >= tuning.difficulty_up_normal_hard and current_difficulty == "normal":
        return "hard"
    elif avg <= tuning.difficulty_down_hard_normal and current_difficulty == "hard":
        return "normal"
    elif avg <= tuning.difficulty_down_normal_easy and current_difficulty == "normal":
        return "easy"
    return current_difficulty


class TestDifficultyAdjustment:

    # --- Upward transitions ---

    def test_normal_to_hard(self):
        scores = [9] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "hard"

    def test_easy_to_normal(self):
        scores = [8] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "easy") == "normal"

    # --- Downward transitions ---

    def test_hard_to_normal(self):
        scores = [3] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "hard") == "normal"

    def test_normal_to_easy(self):
        scores = [2, 3, 4, 3, 2, 3, 4][:tuning.difficulty_recent_window]
        assert _auto_adjust(scores, "normal") == "easy"

    # --- No-change cases ---

    def test_no_change_already_hard(self):
        scores = [9] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "hard") == "hard"

    def test_no_change_already_easy(self):
        scores = [2] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "easy") == "easy"

    def test_no_change_with_few_scores(self):
        scores = [9, 9, 9]
        assert _auto_adjust(scores, "normal") == "normal"

    def test_no_change_middle_range_normal(self):
        """Avg in middle range — no transition from normal."""
        scores = [6] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "normal"

    def test_no_change_middle_range_hard(self):
        """Avg between down-threshold and up-threshold — hard stays hard."""
        scores = [6] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "hard") == "hard"

    def test_no_change_middle_range_easy(self):
        """Avg between down-threshold and up-threshold — easy stays easy."""
        scores = [6] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "easy") == "easy"

    # --- Boundary tests using actual thresholds ---

    def test_boundary_up_normal_hard_exact(self):
        """Exactly at difficulty_up_normal_hard threshold."""
        scores = [tuning.difficulty_up_normal_hard] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "hard"

    def test_boundary_up_normal_hard_below(self):
        """Just below difficulty_up_normal_hard threshold."""
        val = tuning.difficulty_up_normal_hard - 0.1
        scores = [val] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "normal"

    def test_boundary_up_easy_normal_exact(self):
        """Exactly at difficulty_up_easy_normal threshold."""
        scores = [tuning.difficulty_up_easy_normal] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "easy") == "normal"

    def test_boundary_down_normal_easy_exact(self):
        """Exactly at difficulty_down_normal_easy threshold."""
        scores = [tuning.difficulty_down_normal_easy] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "easy"

    def test_boundary_down_normal_easy_above(self):
        """Just above difficulty_down_normal_easy threshold."""
        val = tuning.difficulty_down_normal_easy + 0.1
        scores = [val] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "normal") == "normal"

    def test_boundary_down_hard_normal_exact(self):
        """Exactly at difficulty_down_hard_normal threshold."""
        scores = [tuning.difficulty_down_hard_normal] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "hard") == "normal"

    # --- Priority: easy→normal check runs before normal→hard ---

    def test_easy_promotes_not_to_hard(self):
        """Even if avg exceeds normal→hard threshold, easy only goes to normal."""
        scores = [9] * tuning.difficulty_recent_window
        assert _auto_adjust(scores, "easy") == "normal"
