import json
import time
from typing import Any

from claude_agent_sdk import HookMatcher
from loguru import logger

from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.utils import strip_mcp_prefix

# Fraction of max_turns that must remain to trigger the wrap-up warning.
# Used by both hooks (UserPromptSubmit injection) and session_manager
# (user-facing message). Defined here to avoid circular imports.
TURN_LIMIT_WARN_FRACTION = 0.2


def _extract_score(tool_output: Any) -> int | None:
    """Extract score from record_exercise_result tool output.

    Handles SDK output envelope variations to avoid silent breakage
    if the SDK changes the response format.

    Known formats:
    - dict: ``{"content": [{"type": "text", "text": "<json>"}]}``
    - list: ``[{"type": "text", "text": "<json>"}]``  (content array directly)
    - str: raw JSON string
    """
    try:
        if isinstance(tool_output, dict):
            # Standard SDK format: {"content": [{"type": "text", "text": "<json>"}]}
            content_list = tool_output.get("content", [])
            if content_list and isinstance(content_list[0], dict):
                text_str = content_list[0].get("text", "{}")
                data = json.loads(text_str)
                return data.get("score")
        if isinstance(tool_output, list):
            # SDK may pass content array directly without wrapping dict
            if tool_output and isinstance(tool_output[0], dict):
                text_str = tool_output[0].get("text", "{}")
                data = json.loads(text_str)
                return data.get("score")
        if isinstance(tool_output, str):
            data = json.loads(tool_output)
            return data.get("score")
    except (ValueError, TypeError, KeyError, IndexError):
        pass
    return None


class SessionHookState:
    """Accumulates hook data during a session."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.tool_calls: list[dict] = []
        self.prompts: list[dict] = []
        self.stop_data: dict | None = None
        self.turn_count: int = 0
        self.max_turns: int = 0
        self.wrap_up_injected: bool = False
        self.exercise_scores: list[int] = []  # Scores from this session for trend
        # Cost budget tracking — synced from ManagedSession before each query()
        self.accumulated_cost: float = 0.0
        self.max_cost_usd: float = 0.0
        self.cost_wrap_up_injected: bool = False
        # Summary enrichment fields
        self.exercise_topics: list[str] = []
        self.exercise_types: list[str] = []
        self.words_added: list[str] = []
        self.words_reviewed: int = 0


def build_session_hooks(user_id: int) -> tuple[dict[str, list[HookMatcher]], SessionHookState]:
    """Build hooks with per-session state captured via closures.

    Each session gets its own hook handler functions that reference a
    specific SessionHookState through closure capture. This prevents
    cross-session data corruption when multiple sessions are active.

    Returns:
        Tuple of (hooks_config, hook_state)
    """
    state = SessionHookState(user_id)

    async def post_tool_use_handler(
        input_data: dict[str, Any],
        tool_use_id: str,
        context: Any,
    ) -> dict[str, Any]:
        """Log tool usage and inject adaptive hints after exercise scoring."""
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})
        tool_output = input_data.get("tool_response", "")

        state.tool_calls.append({
            "tool": tool_name,
            "timestamp": time.time(),
        })

        logger.debug("PostToolUse [user={}]: {} (input: {})", user_id, tool_name, str(tool_input)[:100])

        # Track data for session summary enrichment
        stripped_name = strip_mcp_prefix(tool_name)

        if stripped_name == "add_vocabulary":
            # Only track successful adds (not duplicates or errors)
            is_error = isinstance(tool_output, dict) and tool_output.get("is_error")
            is_dup = False
            if isinstance(tool_output, dict):
                content = tool_output.get("content", [])
                if content and isinstance(content[0], dict):
                    try:
                        parsed = json.loads(content[0].get("text", "{}"))
                        is_dup = parsed.get("status") == "duplicate"
                    except (ValueError, TypeError):
                        pass
            if not is_error and not is_dup:
                word = tool_input.get("word", "")
                if isinstance(word, str) and word.strip():
                    state.words_added.append(word.strip()[:100])

        elif stripped_name == "record_vocabulary_review":
            state.words_reviewed += 1

        # Inject adaptive hints after recording exercise results
        if stripped_name == "record_exercise_result":
            topic = tool_input.get("topic", "")
            if isinstance(topic, str) and topic.strip():
                state.exercise_topics.append(topic.strip()[:100])
            ex_type = tool_input.get("exercise_type", "")
            if isinstance(ex_type, str) and ex_type.strip():
                state.exercise_types.append(ex_type.strip()[:100])

            score = _extract_score(tool_output)
            if score is None and tool_output:
                logger.warning(
                    "Failed to extract score from record_exercise_result output [user={}], "
                    "adaptive hints disabled for this exercise. Output type: {}",
                    user_id, type(tool_output).__name__,
                )
            if score is not None:
                state.exercise_scores.append(score)
                # Use trend from session scores for more stable hints
                recent = state.exercise_scores[-tuning.hook_rolling_avg_window:]
                avg = sum(recent) / len(recent)
                count = len(recent)

                if avg <= tuning.hook_struggling_threshold and count >= 2:
                    hint = (
                        "ADAPTIVE_HINT: Student is struggling on recent exercises. "
                        "Simplify the next exercise, offer encouragement, "
                        "and consider reviewing the basics of this topic."
                    )
                elif avg >= tuning.hook_excelling_threshold and count >= 2:
                    hint = (
                        "ADAPTIVE_HINT: Student is excelling on recent exercises. "
                        "Consider increasing difficulty or introducing a new topic."
                    )
                elif score <= 4:
                    hint = (
                        "ADAPTIVE_HINT: Student struggled with this exercise "
                        "but is doing fine overall. Offer help on this specific topic "
                        "without changing overall difficulty."
                    )
                elif score >= 9:
                    hint = (
                        "ADAPTIVE_HINT: Student did very well on this exercise. "
                        "Good progress. Continue at the current level."
                    )
                else:
                    hint = (
                        "ADAPTIVE_HINT: Moderate result. "
                        "Continue at the current level."
                    )

                return {
                    "continue_": True,
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": hint,
                    },
                }

        return {"continue_": True}

    async def stop_handler(
        input_data: dict[str, Any],
        tool_use_id: str,
        context: Any,
    ) -> dict[str, Any]:
        """Capture session end event for cost tracking and analytics."""
        session_id = input_data.get("session_id")
        state.stop_data = {
            "session_id": session_id,
            "timestamp": time.time(),
        }
        logger.info("Session stopped [user={}]: {}", user_id, session_id)
        return {"continue_": True}

    async def user_prompt_submit_handler(
        input_data: dict[str, Any],
        tool_use_id: str,
        context: Any,
    ) -> dict[str, Any]:
        """Log user prompts and inject wrap-up hint near turn limit."""
        prompt = input_data.get("prompt", "")
        state.prompts.append({
            "text": prompt[:500],
            "timestamp": time.time(),
        })
        logger.debug("UserPromptSubmit [user={}]: {}", user_id, prompt[:80])

        # Inject wrap-up hint when approaching turn limit
        if (
            state.max_turns > 0
            and not state.wrap_up_injected
            and state.turn_count >= state.max_turns * (1 - TURN_LIMIT_WARN_FRACTION)
        ):
            state.wrap_up_injected = True
            remaining = state.max_turns - state.turn_count
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"SESSION_LIMIT: Only {remaining} turns remain in this session. "
                        "Start wrapping up the current exercise or topic. "
                        "Summarize what was covered. Do NOT start new exercises or topics."
                    ),
                },
            }

        # Inject wrap-up hint when approaching cost limit (80% of budget)
        if (
            state.max_cost_usd > 0
            and not state.cost_wrap_up_injected
            and state.accumulated_cost >= state.max_cost_usd * (1 - TURN_LIMIT_WARN_FRACTION)
        ):
            state.cost_wrap_up_injected = True
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "SESSION_LIMIT: This session is running low on resources. "
                        "Finish the current exercise, give brief feedback, and "
                        "wrap up. Do NOT start new exercises or topics."
                    ),
                },
            }

        return {"continue_": True}

    hooks = {
        "PostToolUse": [
            HookMatcher(
                matcher=None,  # All tools
                hooks=[post_tool_use_handler],
                timeout=10.0,
            ),
        ],
        "Stop": [
            HookMatcher(
                matcher=None,
                hooks=[stop_handler],
                timeout=10.0,
            ),
        ],
        "UserPromptSubmit": [
            HookMatcher(
                matcher=None,
                hooks=[user_prompt_submit_handler],
                timeout=10.0,
            ),
        ],
    }

    return hooks, state
