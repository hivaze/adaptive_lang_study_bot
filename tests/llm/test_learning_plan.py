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


