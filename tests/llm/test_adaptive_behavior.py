"""Category C: Adaptive behavior — hooks and hints work in practice.

These tests verify that the PostToolUse adaptive hints and the
weak/strong area tracking logic fire correctly through real SDK sessions.
"""

import pytest

from adaptive_lang_study_bot.db.repositories import UserRepo

pytestmark = [pytest.mark.llm, pytest.mark.timeout(60)]


async def test_weak_area_tracking(create_llm_session):
    """Low-scoring exercise on a topic should keep it in weak_areas."""
    session = await create_llm_session(
        max_turns=4,
        user_overrides={
            "weak_areas": ["verb conjugation"],
            "level": "A2",
        },
    )

    await session.query_and_collect(
        "I just struggled through a verb conjugation exercise. "
        "I got most answers wrong. "
        "Record this result: exercise_type='conjugation_drill', "
        "topic='verb conjugation', score=2, max_score=10. "
        "Use the record_exercise_result tool."
    )

    assert "record_exercise_result" in session.bare_tools, (
        f"Expected record_exercise_result called, got: {session.bare_tools}"
    )

    # Weak area should still be present (low score doesn't remove it)
    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    # The tool may add/modify weak_areas but "verb conjugation" should remain
    # since the score was very low on that topic
    weak_lower = [w.lower() for w in user.weak_areas]
    assert any("verb" in w or "conjug" in w for w in weak_lower), (
        f"Expected verb conjugation to remain in weak_areas, got: {user.weak_areas}"
    )
