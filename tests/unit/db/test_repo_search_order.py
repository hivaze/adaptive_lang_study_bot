"""Verify vocabulary search queries include ORDER BY for deterministic results."""

import inspect

from adaptive_lang_study_bot.db.repositories import VocabularyRepo


class TestVocabularySearchOrdering:

    def test_search_has_order_by(self):
        """VocabularyRepo.search() must use ORDER BY for deterministic results with LIMIT."""
        source = inspect.getsource(VocabularyRepo.search)
        assert "order_by" in source, "VocabularyRepo.search() needs ORDER BY with LIMIT"

    def test_get_by_topic_has_order_by(self):
        source = inspect.getsource(VocabularyRepo.get_by_topic)
        assert "order_by" in source, "VocabularyRepo.get_by_topic() needs ORDER BY with LIMIT"
