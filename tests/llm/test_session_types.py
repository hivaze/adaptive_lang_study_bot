"""Category B: Session type behavior — tool access per session type.

These tests verify that different session types (onboarding, interactive,
proactive) restrict tool access correctly and produce appropriate behavior.
"""

import pytest

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
                        "get_session_history"}
    called_restricted = restricted_tools & set(session.bare_tools)
    assert not called_restricted, (
        f"Onboarding session should not use restricted tools, but called: {called_restricted}"
    )


async def test_proactive_sessions_are_tool_less(create_llm_session):
    """Proactive sessions have no tools — they generate text directly."""
    session = await create_llm_session(
        session_type="proactive_nudge",
        max_turns=3,
        system_prompt_override=(
            "You are an automated proactive nudge bot. "
            "Generate a short motivating message for a language learner."
        ),
    )

    await session.query_and_collect(
        "The user hasn't studied in 2 days. Their streak is at risk. "
        "Write a motivating nudge message."
    )

    assert len(session.bare_tools) == 0, (
        f"Proactive sessions should have no tools, but called: {session.bare_tools}"
    )
    assert len(session.response_text) > 0, "Proactive session should produce text output"
