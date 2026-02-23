"""Category A: Tool calling compliance — does Claude call the right tools?

These tests verify that the model reliably invokes the expected MCP tools
when instructed, and that those tool calls produce correct DB state changes.
"""

import pytest

from adaptive_lang_study_bot.db.repositories import (
    ExerciseResultRepo,
    ScheduleRepo,
    UserRepo,
    VocabularyRepo,
)

pytestmark = [pytest.mark.llm, pytest.mark.timeout(60)]


async def test_exercise_triggers_record_exercise_result(create_llm_session):
    """Agent MUST call record_exercise_result when told a student finished an exercise."""
    session = await create_llm_session(max_turns=4)

    await session.query_and_collect(
        "I just completed a vocabulary quiz on basic Spanish greetings. "
        "Here are the results: I translated hola, adiós, and gracias correctly. "
        "My score is 9 out of 10, exercise type is 'vocabulary_quiz', topic is 'greetings'. "
        "Please record this exercise result now using the record_exercise_result tool."
    )

    assert "record_exercise_result" in session.bare_tools, (
        f"Expected record_exercise_result to be called, got: {session.bare_tools}"
    )

    # Verify DB state: exercise result row exists
    results = await ExerciseResultRepo.get_recent(
        session.db_session, session.user.telegram_id, limit=5,
    )
    assert len(results) >= 1, "Expected at least 1 exercise result row in DB"

    # Verify user's recent_scores updated
    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    assert user.recent_scores, "Expected recent_scores to be non-empty after exercise"


async def test_teaching_triggers_add_vocabulary(create_llm_session):
    """Agent MUST call add_vocabulary when teaching new words."""
    session = await create_llm_session()

    await session.query_and_collect(
        "Teach me 3 new Spanish words about food with their translations. "
        "Make sure to save them to my vocabulary using the add_vocabulary tool."
    )

    assert "add_vocabulary" in session.bare_tools, (
        f"Expected add_vocabulary to be called, got: {session.bare_tools}"
    )

    # Verify DB state: vocabulary rows created
    count = await VocabularyRepo.count_for_user(
        session.db_session, session.user.telegram_id,
    )
    assert count > 0, "Expected vocabulary count > 0 after teaching words"


async def test_vocabulary_search(create_llm_session):
    """Agent should call search_vocabulary when asked to look up existing words."""
    session = await create_llm_session(max_turns=6)

    # First teach some words so there's something to search
    await session.query_and_collect(
        "Teach me the Spanish word for 'water' (agua). "
        "Save it using add_vocabulary."
    )

    assert "add_vocabulary" in session.bare_tools, (
        f"Expected add_vocabulary called first, got: {session.bare_tools}"
    )

    # Now search for it
    tools_before = list(session.tools_called)
    await session.query_and_collect(
        "Search my vocabulary for the word 'agua' using the search_vocabulary tool."
    )

    new_tools = session.tools_called[len(tools_before):]
    new_bare = [name.removeprefix("mcp__langbot__") for name in new_tools]
    assert "search_vocabulary" in new_bare, (
        f"Expected search_vocabulary called, got new tools: {new_bare}"
    )


async def test_schedule_creation(create_llm_session):
    """Agent should create a schedule when the user asks for reminders."""
    session = await create_llm_session()

    await session.query_and_collect(
        "Set up a daily study reminder for me at 9am. "
        "Use the manage_schedule tool to create it."
    )

    assert "manage_schedule" in session.bare_tools, (
        f"Expected manage_schedule to be called, got: {session.bare_tools}"
    )

    # Verify DB state: schedule row created
    count = await ScheduleRepo.count_for_user(
        session.db_session, session.user.telegram_id,
    )
    assert count >= 1, "Expected at least 1 schedule row in DB"


async def test_preference_update(create_llm_session):
    """Agent should call update_preference when the user changes preferences."""
    session = await create_llm_session(
        user_overrides={"session_style": "structured", "interests": []},
    )

    await session.query_and_collect(
        "I'd like to switch to a casual learning style. "
        "Also, I'm really interested in movies and music. "
        "Please save these preferences using update_preference."
    )

    assert "update_preference" in session.bare_tools, (
        f"Expected update_preference to be called, got: {session.bare_tools}"
    )

    # Verify DB state: preferences updated
    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    # At least one of these should have changed
    style_changed = user.session_style == "casual"
    interests_changed = bool(user.interests)
    assert style_changed or interests_changed, (
        f"Expected style='casual' or interests non-empty, "
        f"got style={user.session_style!r}, interests={user.interests!r}"
    )
