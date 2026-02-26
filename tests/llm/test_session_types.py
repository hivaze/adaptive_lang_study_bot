"""Category B: Session type behavior — tool access per session type.

These tests verify that different session types (onboarding, interactive,
proactive) restrict tool access correctly and produce appropriate behavior.
"""

from unittest.mock import MagicMock

import pytest

from adaptive_lang_study_bot.agent.prompt_builder import build_proactive_prompt

pytestmark = [pytest.mark.llm, pytest.mark.timeout(60)]


async def test_onboarding_uses_limited_tools(create_llm_session):
    """Onboarding sessions should only use the 3 allowed tools."""
    session = await create_llm_session(
        session_type="onboarding",
        user_overrides={"onboarding_completed": False, "interests": []},
    )

    await session.query_and_collect(
        "Hi! I'm new here. I want to learn Spanish. "
        "I'm interested in cooking and travel. My native language is English. "
        "Please set up my profile preferences."
    )

    # Onboarding allows: get_user_profile, update_preference, manage_schedule
    restricted_tools = {"record_exercise_result", "add_vocabulary",
                        "get_due_vocabulary", "search_vocabulary",
                        "get_exercise_history", "send_notification"}
    called_restricted = restricted_tools & set(session.bare_tools)
    assert not called_restricted, (
        f"Onboarding session should not use restricted tools, but called: {called_restricted}"
    )


async def test_interactive_no_send_notification(create_llm_session):
    """Interactive sessions should NOT have access to send_notification."""
    session = await create_llm_session(session_type="interactive")

    await session.query_and_collect(
        "Send me a reminder notification about studying tomorrow. "
        "Use the send_notification tool."
    )

    assert "send_notification" not in session.bare_tools, (
        "Interactive session should not call send_notification"
    )


async def test_proactive_nudge_sends_notification(create_llm_session):
    """Proactive nudge sessions should call send_notification."""
    session = await create_llm_session(
        session_type="proactive_nudge",
        max_turns=3,
        system_prompt_override=(
            "You are an automated proactive nudge bot. "
            "Your ONLY job is to send a short motivating message to the user. "
            "You MUST call the send_notification tool with a brief message. "
            "Do not do anything else — just send the notification."
        ),
    )

    await session.query_and_collect(
        "The user hasn't studied in 2 days. Their streak is at risk. "
        "Send them a motivating nudge via send_notification."
    )

    assert "send_notification" in session.bare_tools, (
        f"Proactive nudge should call send_notification, got: {session.bare_tools}"
    )


async def test_proactive_review_loads_due_vocab(create_llm_session):
    """Proactive review sessions should load due vocab and send notification."""
    session = await create_llm_session(
        session_type="proactive_review",
        max_turns=5,
        system_prompt_override=(
            "You are an automated vocabulary review bot. "
            "You MUST perform these steps IN ORDER:\n"
            "1. Call get_due_vocabulary to check for due cards.\n"
            "2. Call send_notification with a summary of the due cards "
            "(or a message that no cards are due).\n"
            "Do NOT skip any step. Call each tool exactly once."
        ),
    )

    await session.query_and_collect(
        "Run the automated vocabulary review. "
        "First call get_due_vocabulary, then call send_notification with the results."
    )

    assert "get_due_vocabulary" in session.bare_tools, (
        f"Proactive review should call get_due_vocabulary, got: {session.bare_tools}"
    )
    assert "send_notification" in session.bare_tools, (
        f"Proactive review should call send_notification, got: {session.bare_tools}"
    )

    # Restricted tools for proactive_review should NOT be called
    restricted = {"record_exercise_result", "add_vocabulary", "manage_schedule",
                  "update_preference", "search_vocabulary", "get_exercise_history"}
    called_restricted = restricted & set(session.bare_tools)
    assert not called_restricted, (
        f"Proactive review used restricted tools: {called_restricted}"
    )


async def test_proactive_quiz_records_exercise(create_llm_session):
    """Proactive quiz sessions should record exercise results and send notification."""
    session = await create_llm_session(
        session_type="proactive_quiz",
        max_turns=5,
        system_prompt_override=(
            "You are an automated quiz bot. "
            "You MUST perform these steps IN ORDER:\n"
            "1. Call record_exercise_result with exercise_type='automated_quiz', "
            "topic='greetings', score=7, max_score=10.\n"
            "2. Call send_notification with a brief quiz result summary.\n"
            "Do NOT skip any step. Call each tool exactly once."
        ),
    )

    await session.query_and_collect(
        "Run an automated quiz. Record the exercise result "
        "(exercise_type='automated_quiz', topic='greetings', score=7, max_score=10), "
        "then send a notification with the result summary."
    )

    assert "record_exercise_result" in session.bare_tools, (
        f"Proactive quiz should call record_exercise_result, got: {session.bare_tools}"
    )
    assert "send_notification" in session.bare_tools, (
        f"Proactive quiz should call send_notification, got: {session.bare_tools}"
    )

    # Restricted tools for proactive_quiz should NOT be called
    restricted = {"add_vocabulary", "get_due_vocabulary",
                  "manage_schedule", "update_preference",
                  "search_vocabulary", "get_exercise_history"}
    called_restricted = restricted & set(session.bare_tools)
    assert not called_restricted, (
        f"Proactive quiz used restricted tools: {called_restricted}"
    )


async def test_proactive_summary_loads_history(create_llm_session):
    """Proactive summary sessions should load exercise history and send notification."""
    session = await create_llm_session(
        session_type="proactive_summary",
        max_turns=5,
        system_prompt_override=(
            "You are an automated progress summary bot. "
            "You MUST perform these steps IN ORDER:\n"
            "1. Call get_exercise_history to get recent exercise data.\n"
            "2. Call send_notification with a progress summary based on the data "
            "(or a message that no exercises were found).\n"
            "Do NOT skip any step. Call each tool exactly once."
        ),
    )

    await session.query_and_collect(
        "Run the automated progress summary. "
        "First call get_exercise_history, then send a notification with the summary."
    )

    assert "get_exercise_history" in session.bare_tools, (
        f"Proactive summary should call get_exercise_history, got: {session.bare_tools}"
    )
    assert "send_notification" in session.bare_tools, (
        f"Proactive summary should call send_notification, got: {session.bare_tools}"
    )

    # Restricted tools for proactive_summary should NOT be called
    restricted = {"record_exercise_result", "add_vocabulary", "get_due_vocabulary",
                  "manage_schedule", "update_preference", "search_vocabulary"}
    called_restricted = restricted & set(session.bare_tools)
    assert not called_restricted, (
        f"Proactive summary used restricted tools: {called_restricted}"
    )


async def test_proactive_nudge_with_production_prompt(create_llm_session):
    """build_proactive_prompt() produces a prompt that drives send_notification."""
    # Build prompt from a mock user matching create_llm_session defaults
    mock_user = MagicMock()
    mock_user.first_name = "TestStudent"
    mock_user.native_language = "en"
    mock_user.target_language = "es"
    mock_user.level = "A2"
    mock_user.streak_days = 5
    mock_user.vocabulary_count = 0
    mock_user.interests = ["cooking", "travel"]
    mock_user.learning_goals = []
    mock_user.weak_areas = []
    mock_user.recent_scores = []
    mock_user.topics_to_avoid = []

    prompt = build_proactive_prompt(
        mock_user, "proactive_nudge", {"streak": 5},
    )

    session = await create_llm_session(
        session_type="proactive_nudge",
        max_turns=5,
        system_prompt_override=prompt,
    )

    await session.query_and_collect("Execute your proactive task now.")

    assert "send_notification" in session.bare_tools, (
        f"Production proactive prompt should drive send_notification, got: {session.bare_tools}"
    )
