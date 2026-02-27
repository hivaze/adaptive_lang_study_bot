"""Verify add_vocabulary uses atomic increment instead of COUNT(*)."""

import inspect

from adaptive_lang_study_bot.agent.tools import create_session_tools


class TestVocabCountEfficiency:

    def test_add_vocabulary_uses_atomic_increment(self):
        """add_vocabulary tool should use SQL-level atomic increment."""
        source = inspect.getsource(create_session_tools)
        assert "vocabulary_count" in source and "+ 1" in source, (
            "add_vocabulary should use User.vocabulary_count + 1 for atomic increment"
        )
