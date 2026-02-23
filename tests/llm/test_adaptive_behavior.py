"""Category C: Adaptive behavior — hooks and hints work in practice.

These tests verify that the PostToolUse adaptive hints and the automatic
level adjustment logic fire correctly through real SDK sessions.
"""

import pytest

from adaptive_lang_study_bot.db.repositories import UserRepo

pytestmark = [pytest.mark.llm, pytest.mark.timeout(60)]


async def test_low_score_hint_injected(create_llm_session):
    """After recording a low score, the PostToolUse hook should inject an adaptive hint."""
    session = await create_llm_session(
        max_turns=4,
        user_overrides={"recent_scores": [3, 2, 4, 3, 2], "level": "A2"},
    )

    await session.query_and_collect(
        "I just finished a vocabulary exercise on Spanish greetings. "
        "I struggled and got most wrong. "
        "Please record this result now: exercise_type='vocabulary_quiz', "
        "topic='greetings', score=3, max_score=10. "
        "Use the record_exercise_result tool."
    )

    # Verify the hook logged tool calls
    hook_tool_names = [tc["tool"] for tc in session.hook_state.tool_calls]
    stripped_hook_tools = [
        name.removeprefix("mcp__langbot__") for name in hook_tool_names
    ]
    assert "record_exercise_result" in stripped_hook_tools, (
        f"Expected record_exercise_result in hook logs, got: {stripped_hook_tools}"
    )

    # Supplementary: response should contain encouraging/simplifying language
    response_lower = session.response_text.lower()
    encouraging_keywords = [
        "simpl", "basic", "easier", "don't worry", "great start",
        "good", "keep", "try", "practice", "review", "let's",
        "improve", "progress", "next time", "encourage",
    ]
    assert any(kw in response_lower for kw in encouraging_keywords), (
        "Expected encouraging language after low score"
    )


async def test_high_score_triggers_level_up(create_llm_session):
    """When avg(last 5 scores) >= 9.0, the tool should auto-adjust level up."""
    session = await create_llm_session(
        max_turns=4,
        user_overrides={
            "level": "A1",
            "recent_scores": [9, 10, 9, 10],  # Need 1 more score >= 9 to trigger
        },
    )

    await session.query_and_collect(
        "I just completed a translation exercise on basic greetings. "
        "I got everything right — perfect score! "
        "Please record this result: exercise_type='translation', "
        "topic='greetings', score=10, max_score=10. "
        "Use the record_exercise_result tool."
    )

    assert "record_exercise_result" in session.bare_tools, (
        f"Expected record_exercise_result called, got: {session.bare_tools}"
    )

    # The record_exercise_result tool auto-adjusts level when avg(last 5) >= 9.0
    # With scores [9, 10, 9, 10, 10], avg = 9.6 → level should go from A1 to A2
    user = await UserRepo.get(session.db_session, session.user.telegram_id)
    assert user.recent_scores, "Expected scores to be recorded"
    if len(user.recent_scores) >= 5:
        avg = sum(user.recent_scores[-5:]) / 5
        if avg >= 9.0:
            assert user.level == "A2", (
                f"Expected level A2 after avg={avg:.1f}, got {user.level}"
            )


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
