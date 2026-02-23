"""Verify all ORM relationships use lazy='raise' to prevent MissingGreenlet in async."""

from sqlalchemy import inspect as sa_inspect

from adaptive_lang_study_bot.db.models import (
    ExerciseResult,
    Notification,
    Schedule,
    Session,
    User,
    Vocabulary,
    VocabularyReviewLog,
)


class TestRelationshipLazyMode:

    def test_all_relationships_are_raise(self):
        """Every relationship on every model must use lazy='raise'."""
        models = [User, Vocabulary, Session, Schedule, ExerciseResult, Notification, VocabularyReviewLog]
        for model in models:
            mapper = sa_inspect(model)
            for rel in mapper.relationships:
                assert rel.lazy == "raise", (
                    f"{model.__name__}.{rel.key} uses lazy='{rel.lazy}', expected 'raise'"
                )
