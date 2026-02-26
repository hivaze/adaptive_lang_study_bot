"""LLM tests for the manage_learning_plan tool.

Tests verify that Claude correctly calls the tool when instructed,
and that session-type restrictions are enforced.
"""

import pytest

from adaptive_lang_study_bot.db.repositories import LearningPlanRepo

pytestmark = [pytest.mark.llm, pytest.mark.timeout(90)]


async def test_plan_creation_triggers_manage_learning_plan(create_llm_session):
    """Agent MUST call manage_learning_plan(action='create') when asked to create a plan."""
    session = await create_llm_session(
        max_turns=8,
        user_overrides={
            "level": "A2",
            "sessions_completed": 5,
            "interests": ["cooking", "travel"],
        },
    )

    await session.query_and_collect(
        "I want to create a 4-week learning plan to reach B1 level. "
        "I can study 3-4 times per week. "
        "Please create the plan now using manage_learning_plan with action='create'. "
        "Include 2-3 topics per week with vocabulary themes."
    )

    assert "manage_learning_plan" in session.bare_tools, (
        f"Expected manage_learning_plan to be called, got: {session.bare_tools}"
    )

    # Verify DB state: plan row exists
    plan = await LearningPlanRepo.get_active(
        session.db_session, session.user.telegram_id,
    )
    assert plan is not None, "Expected an active learning plan in DB after creation"
    assert plan.current_level == "A2"
    assert plan.target_level == "B1"


async def test_plan_get_triggers_manage_learning_plan(create_llm_session):
    """Agent MUST call manage_learning_plan(action='get') to check plan progress."""
    session = await create_llm_session(
        max_turns=5,
        user_overrides={"level": "A2", "sessions_completed": 5},
    )

    await session.query_and_collect(
        "Check my learning plan progress. "
        "Use manage_learning_plan with action='get' to retrieve my current plan."
    )

    assert "manage_learning_plan" in session.bare_tools, (
        f"Expected manage_learning_plan to be called, got: {session.bare_tools}"
    )


async def test_proactive_summary_can_read_plan(create_llm_session):
    """Proactive summary sessions should be able to call manage_learning_plan(action='get')."""
    session = await create_llm_session(
        session_type="proactive_summary",
        max_turns=5,
        system_prompt_override=(
            "You are an automated progress summary bot. "
            "You MUST perform these steps IN ORDER:\n"
            "1. Call manage_learning_plan with action='get' to check learning plan.\n"
            "2. Call send_notification with a summary including plan progress "
            "(or a message that no plan exists).\n"
            "Do NOT skip any step. Call each tool exactly once."
        ),
    )

    await session.query_and_collect(
        "Run the automated progress summary. "
        "First call manage_learning_plan(action='get'), "
        "then send a notification with the summary."
    )

    assert "manage_learning_plan" in session.bare_tools, (
        f"Proactive summary should call manage_learning_plan, got: {session.bare_tools}"
    )
    assert "send_notification" in session.bare_tools, (
        f"Proactive summary should call send_notification, got: {session.bare_tools}"
    )


async def test_proactive_nudge_cannot_use_manage_learning_plan(create_llm_session):
    """Proactive nudge sessions should NOT have access to manage_learning_plan."""
    session = await create_llm_session(
        session_type="proactive_nudge",
        max_turns=3,
        system_prompt_override=(
            "You are a proactive nudge bot. "
            "You can only use get_user_profile and send_notification. "
            "If asked to manage a learning plan, explain that you cannot."
        ),
    )

    await session.query_and_collect(
        "Check the student's learning plan using manage_learning_plan."
    )

    assert "manage_learning_plan" not in session.bare_tools, (
        f"Proactive nudge should not call manage_learning_plan, got: {session.bare_tools}"
    )


async def test_onboarding_cannot_use_manage_learning_plan(create_llm_session):
    """Onboarding sessions should NOT have access to manage_learning_plan."""
    session = await create_llm_session(
        session_type="onboarding",
        max_turns=3,
        user_overrides={"onboarding_completed": False},
        system_prompt_override=(
            "You are an onboarding assistant. "
            "Help new users set up their profile. "
            "If asked about learning plans, explain that plans are available after onboarding."
        ),
    )

    await session.query_and_collect(
        "Create a learning plan for me using manage_learning_plan."
    )

    assert "manage_learning_plan" not in session.bare_tools, (
        f"Onboarding session should not call manage_learning_plan, got: {session.bare_tools}"
    )
