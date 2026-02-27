"""Category F: Multi-turn conversation — session continuity across turns.

These tests verify that the SDK session maintains context between
multiple query/response cycles within the same session.
"""

import pytest

from adaptive_lang_study_bot.db.repositories import UserRepo, VocabularyRepo

pytestmark = [pytest.mark.llm, pytest.mark.timeout(90)]


async def test_teach_then_quiz(create_llm_session):
    """Turn 1: teach words → Turn 2: record exercise result for a quiz."""
    session = await create_llm_session(max_turns=8)

    # Turn 1: Teach vocabulary
    await session.query_and_collect(
        "Teach me 2 new Spanish words about food with translations. "
        "Save them to my vocabulary using add_vocabulary."
    )

    assert "add_vocabulary" in session.bare_tools, (
        f"Turn 1: expected add_vocabulary called, got: {session.bare_tools}"
    )

    # Turn 2: Report quiz results and ask for recording
    tools_before_t2 = list(session.tools_called)
    await session.query_and_collect(
        "I just quizzed myself on the words you taught me. "
        "I got one right and one wrong — score 5 out of 10. "
        "Please record this exercise result: exercise_type='vocabulary_quiz', "
        "topic='food', score=5, max_score=10. "
        "Use the record_exercise_result tool."
    )

    # Check for new tool calls in turn 2
    new_tools = session.tools_called[len(tools_before_t2):]
    new_bare = [name.removeprefix("mcp__langbot__") for name in new_tools]
    assert "record_exercise_result" in new_bare, (
        f"Turn 2: expected record_exercise_result called, got new tools: {new_bare}"
    )

    # Verify both vocabulary and exercise results in DB
    vocab_count = await VocabularyRepo.count_for_user(
        session.db_session, session.user.telegram_id,
    )
    assert vocab_count > 0, "Expected vocabulary to be persisted"

    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    assert user.recent_scores, "Expected scores to be recorded"


async def test_preference_then_exercise(create_llm_session):
    """Turn 1: update preferences → Turn 2: exercise uses those preferences."""
    session = await create_llm_session(
        max_turns=8,
        user_overrides={"interests": []},
    )

    # Turn 1: Set preferences
    await session.query_and_collect(
        "I'm really interested in sports. "
        "Please save 'sports' as my interest using update_preference."
    )

    assert "update_preference" in session.bare_tools, (
        f"Turn 1: expected update_preference called, got: {session.bare_tools}"
    )

    # Verify preference was saved
    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    assert user.interests, "Expected interests to be updated"

    # Turn 2: Exercise should reference the interest
    session.response_text = ""
    await session.query_and_collect(
        "Now give me a quick vocabulary exercise about one of my interests."
    )

    response_lower = session.response_text.lower()
    # The exercise should reference sports (the saved interest)
    sports_keywords = ["sport", "game", "play", "team", "match", "ball", "athlete",
                       "deporte", "jugar", "equipo", "partido"]
    has_sports = any(kw in response_lower for kw in sports_keywords)
    assert has_sports, (
        "Expected exercise to reference sports (the saved interest), "
        f"but response was: {session.response_text[:300]}"
    )
