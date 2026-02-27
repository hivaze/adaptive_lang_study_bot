"""Tests for hooks — PostToolUse adaptive hints, wrap-up injection, state tracking.

Tests the actual build_session_hooks() code from hooks.py, not a mirror function.
"""

import json

import pytest

from adaptive_lang_study_bot.agent.hooks import build_session_hooks


# ---------------------------------------------------------------------------
# Tool output parsing (existing tests, updated to use production code)
# ---------------------------------------------------------------------------


def _make_exercise_tool_output(score: int) -> dict:
    """Build a tool_response dict mimicking what the SDK delivers."""
    return {
        "content": [
            {"type": "text", "text": json.dumps({"score": score, "status": "recorded"})},
        ],
    }


class TestToolOutputParsing:
    """PostToolUse hook correctly extracts scores from tool_response."""

    @pytest.fixture()
    def _hook_handler(self):
        """Return the PostToolUse hook handler from build_session_hooks."""
        hooks, state = build_session_hooks(user_id=1)
        handler = hooks["PostToolUse"][0].hooks[0]
        return handler, state

    @pytest.mark.asyncio
    async def test_extracts_score_from_dict_output(self, _hook_handler):
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {"score": 8},
                "tool_response": _make_exercise_tool_output(8),
            },
            "test-id",
            None,
        )
        # Should inject adaptive hint
        assert "hookSpecificOutput" in result
        assert "ADAPTIVE_HINT" in result["hookSpecificOutput"]["additionalContext"]

    @pytest.mark.asyncio
    async def test_low_score_hint_single(self, _hook_handler):
        """Single low score gives per-exercise hint, not struggling trend."""
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": _make_exercise_tool_output(3),
            },
            "test-id",
            None,
        )
        hint = result["hookSpecificOutput"]["additionalContext"]
        assert "struggled" in hint
        assert "this exercise" in hint

    @pytest.mark.asyncio
    async def test_struggling_trend_hint(self, _hook_handler):
        """Struggling hint requires 2+ low scores (not just one)."""
        handler, _ = _hook_handler
        for i in range(2):
            result = await handler(
                {
                    "tool_name": "mcp__langbot__record_exercise_result",
                    "tool_input": {},
                    "tool_response": _make_exercise_tool_output(3),
                },
                f"test-id-{i}",
                None,
            )
        hint = result["hookSpecificOutput"]["additionalContext"]
        assert "Simplify" in hint
        assert "struggling" in hint

    @pytest.mark.asyncio
    async def test_high_score_trend_hint(self, _hook_handler):
        """Increasing difficulty hint only triggers after 2+ high scores (trend)."""
        handler, state = _hook_handler
        # First high score — not enough for "increase difficulty" trend
        result1 = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": _make_exercise_tool_output(10),
            },
            "test-id-1",
            None,
        )
        hint1 = result1["hookSpecificOutput"]["additionalContext"]
        assert "very well" in hint1
        assert "current level" in hint1

        # Second high score — now trend triggers
        result2 = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": _make_exercise_tool_output(9),
            },
            "test-id-2",
            None,
        )
        hint2 = result2["hookSpecificOutput"]["additionalContext"]
        assert "excelling" in hint2
        assert "increasing difficulty" in hint2

    @pytest.mark.asyncio
    async def test_average_score_hint(self, _hook_handler):
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": _make_exercise_tool_output(6),
            },
            "test-id",
            None,
        )
        hint = result["hookSpecificOutput"]["additionalContext"]
        assert "current level" in hint
        assert "Moderate" in hint

    @pytest.mark.asyncio
    async def test_no_hint_for_non_exercise_tools(self, _hook_handler):
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__add_vocabulary",
                "tool_input": {},
                "tool_response": {"content": [{"type": "text", "text": "{}"}]},
            },
            "test-id",
            None,
        )
        assert "hookSpecificOutput" not in result
        assert result["continue_"] is True

    @pytest.mark.asyncio
    async def test_empty_tool_response_doesnt_crash(self, _hook_handler):
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": "",
            },
            "test-id",
            None,
        )
        # Should not crash, just no hint
        assert result["continue_"] is True

    @pytest.mark.asyncio
    async def test_missing_tool_response_key(self, _hook_handler):
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                # No tool_response key at all
            },
            "test-id",
            None,
        )
        assert result["continue_"] is True

    @pytest.mark.asyncio
    async def test_error_tool_response(self, _hook_handler):
        """Error responses (plain text, not JSON) should not crash."""
        handler, _ = _hook_handler
        result = await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {},
                "tool_response": {
                    "content": [{"type": "text", "text": "Error: user not found"}],
                    "is_error": True,
                },
            },
            "test-id",
            None,
        )
        assert result["continue_"] is True


# ---------------------------------------------------------------------------
# Hook state tracking
# ---------------------------------------------------------------------------


class TestSessionHookState:

    @pytest.mark.asyncio
    async def test_stop_tracked(self):
        hooks, state = build_session_hooks(user_id=1)
        handler = hooks["Stop"][0].hooks[0]

        await handler({"session_id": "abc-123"}, "id1", None)

        assert state.stop_data is not None
        assert state.stop_data["session_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Wrap-up injection
# ---------------------------------------------------------------------------


class TestWrapUpInjection:

    @pytest.mark.asyncio
    async def test_no_injection_before_threshold(self):
        hooks, state = build_session_hooks(user_id=1)
        state.max_turns = 20
        state.turn_count = 10  # 50% — well below 80%
        handler = hooks["UserPromptSubmit"][0].hooks[0]

        result = await handler({"prompt": "test"}, "id1", None)
        assert "hookSpecificOutput" not in result

    @pytest.mark.asyncio
    async def test_injection_at_threshold(self):
        hooks, state = build_session_hooks(user_id=1)
        state.max_turns = 20
        state.turn_count = 16  # 80% = 20 * (1 - 0.2)
        handler = hooks["UserPromptSubmit"][0].hooks[0]

        result = await handler({"prompt": "test"}, "id1", None)
        assert "hookSpecificOutput" in result
        assert "SESSION_LIMIT" in result["hookSpecificOutput"]["additionalContext"]
        assert "4 turns remain" in result["hookSpecificOutput"]["additionalContext"]

    @pytest.mark.asyncio
    async def test_injection_only_once(self):
        hooks, state = build_session_hooks(user_id=1)
        state.max_turns = 20
        state.turn_count = 16
        handler = hooks["UserPromptSubmit"][0].hooks[0]

        # First call — injects
        result1 = await handler({"prompt": "test"}, "id1", None)
        assert "hookSpecificOutput" in result1

        # Second call — should NOT inject again
        state.turn_count = 17
        result2 = await handler({"prompt": "test"}, "id2", None)
        assert "hookSpecificOutput" not in result2

    @pytest.mark.asyncio
    async def test_no_injection_when_max_turns_zero(self):
        hooks, state = build_session_hooks(user_id=1)
        state.max_turns = 0  # Not set
        state.turn_count = 100
        handler = hooks["UserPromptSubmit"][0].hooks[0]

        result = await handler({"prompt": "test"}, "id1", None)
        assert "hookSpecificOutput" not in result

