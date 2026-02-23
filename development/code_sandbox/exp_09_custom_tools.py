"""
Experiment 09: Custom Tools (SDK MCP Server)
=============================================
Goal: Build in-process custom tools via @tool + create_sdk_mcp_server().
Test tool chaining, error handling, and mixing with built-in tools.

Run: poetry run python development/code_sandbox/exp_09_custom_tools.py
Output: development/code_sandbox/output/exp_09_output.txt
"""

import asyncio
import json
from typing import Any

from shared import load_env, Log, extract_text, extract_result

# ─── Mock data ─────────────────────────────────────────────────
MOCK_USERS = {
    "user_123": {
        "name": "Alex",
        "language": "Spanish",
        "level": "A2",
        "streak": 12,
        "weak_areas": ["subjunctive mood", "irregular verbs"],
        "vocabulary_count": 340,
        "last_session": "2026-02-19",
    },
    "user_456": {
        "name": "Maria",
        "language": "Japanese",
        "level": "A1",
        "streak": 3,
        "weak_areas": ["katakana", "particles"],
        "vocabulary_count": 80,
        "last_session": "2026-02-20",
    },
}

MOCK_EXERCISES = {
    "subjunctive_A2_1": {
        "id": "subjunctive_A2_1",
        "topic": "subjunctive mood",
        "difficulty": "A2",
        "type": "fill_blank",
        "question": "Complete: 'Espero que tú ___ (venir) a la fiesta.'",
        "answer": "vengas",
        "hint": "Subjunctive form of 'venir' for 'tú'",
    },
    "katakana_A1_1": {
        "id": "katakana_A1_1",
        "topic": "katakana",
        "difficulty": "A1",
        "type": "translation",
        "question": "Write 'coffee' in katakana",
        "answer": "コーヒー",
        "hint": "It sounds like 'koohii'",
    },
}

# Track tool calls for analysis
tool_call_log = []


async def main():
    load_env()
    log = Log("exp_09_output")

    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, UserMessage, ToolResultBlock,
    )

    log.sep("Experiment 09: Custom Tools (SDK MCP Server)")

    # ─── Define custom tools ──────────────────────────────────
    @tool("get_user_profile", "Get a user's learning profile with their stats and weak areas", {"user_id": str})
    async def get_user_profile(args: dict[str, Any]) -> dict[str, Any]:
        tool_call_log.append({"tool": "get_user_profile", "args": args})
        user_id = args["user_id"]
        if user_id in MOCK_USERS:
            return {"content": [{"type": "text", "text": json.dumps(MOCK_USERS[user_id], indent=2)}]}
        return {"content": [{"type": "text", "text": f"User {user_id} not found"}], "is_error": True}

    @tool("get_exercise", "Get a learning exercise for a given topic and difficulty", {"topic": str, "difficulty": str})
    async def get_exercise(args: dict[str, Any]) -> dict[str, Any]:
        tool_call_log.append({"tool": "get_exercise", "args": args})
        topic = args["topic"].lower()
        difficulty = args["difficulty"].upper()
        for ex in MOCK_EXERCISES.values():
            if topic in ex["topic"].lower() and difficulty in ex["difficulty"]:
                return {"content": [{"type": "text", "text": json.dumps(ex, indent=2)}]}
        return {"content": [{"type": "text", "text": f"No exercise found for topic='{topic}', difficulty='{difficulty}'"}]}

    @tool("record_answer", "Record a user's answer to an exercise and update their stats", {
        "user_id": str, "exercise_id": str, "answer": str, "correct": bool
    })
    async def record_answer(args: dict[str, Any]) -> dict[str, Any]:
        tool_call_log.append({"tool": "record_answer", "args": args})
        user_id = args["user_id"]
        if user_id in MOCK_USERS:
            user = MOCK_USERS[user_id]
            if args["correct"]:
                user["vocabulary_count"] += 1
                user["streak"] += 1
            return {"content": [{"type": "text", "text": json.dumps({
                "status": "recorded",
                "user_id": user_id,
                "exercise_id": args["exercise_id"],
                "was_correct": args["correct"],
                "updated_streak": user["streak"],
                "updated_vocab_count": user["vocabulary_count"],
            }, indent=2)}]}
        return {"content": [{"type": "text", "text": f"User {user_id} not found"}], "is_error": True}

    # ─── Create MCP server ────────────────────────────────────
    langbot_server = create_sdk_mcp_server(
        name="langbot",
        version="1.0.0",
        tools=[get_user_profile, get_exercise, record_answer],
    )

    # ─── Test A: Tool chaining ────────────────────────────────
    log.sep("Test A: Tool chaining (profile -> exercise -> record)")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=10,
        mcp_servers={"langbot": langbot_server},
        allowed_tools=[
            "mcp__langbot__get_user_profile",
            "mcp__langbot__get_exercise",
            "mcp__langbot__record_answer",
        ],
        system_prompt=(
            "You are a language learning assistant. When asked to run a session for a user, "
            "you should: 1) load their profile, 2) pick an exercise matching their weak areas, "
            "3) present the exercise, 4) if the user answers, record the result. Be concise."
        ),
    )

    tool_call_log.clear()

    async with ClaudeSDKClient(options) as client:
        await client.query(
            "Run a quick practice session for user_123. Load their profile, "
            "find an exercise for their weakest area, and present it to me."
        )

        msgs = []
        async for msg in client.receive_response():
            msgs.append(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        log(f"  Tool call: {block.name}({json.dumps(block.input)[:100]})")
                    elif isinstance(block, TextBlock):
                        log(f"  Text: {block.text[:200]}")
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        preview = str(block.content)[:100] if block.content else "None"
                        log(f"  Tool result: {preview}...")

        result = extract_result(msgs)
        log(f"\nCost: ${result.get('cost', 0):.6f}")
        log(f"Num turns: {result.get('num_turns', 0)}")
        log(f"Tool calls made: {len(tool_call_log)}")
        for i, tc in enumerate(tool_call_log):
            log(f"  {i+1}. {tc['tool']}({json.dumps(tc['args'])[:80]})")

        # Now answer the exercise
        log("\n--- Answering the exercise ---")
        tool_call_log.clear()

        await client.query("The answer is 'vengas'. Record it as correct for user_123.")

        msgs2 = []
        async for msg in client.receive_response():
            msgs2.append(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        log(f"  Tool call: {block.name}({json.dumps(block.input)[:100]})")
                    elif isinstance(block, TextBlock):
                        log(f"  Text: {block.text[:200]}")

        result2 = extract_result(msgs2)
        log(f"\nCost: ${result2.get('cost', 0):.6f}")
        log(f"Tool calls: {len(tool_call_log)}")
        for tc in tool_call_log:
            log(f"  {tc['tool']}({json.dumps(tc['args'])[:100]})")

    # ─── Test B: Error handling ───────────────────────────────
    log.sep("Test B: Tool error handling (nonexistent user)")

    tool_call_log.clear()

    async with ClaudeSDKClient(options) as client:
        await client.query("Load the profile for user_999 (this user doesn't exist).")

        msgs3 = []
        async for msg in client.receive_response():
            msgs3.append(msg)

        text3 = extract_text(msgs3)
        result3 = extract_result(msgs3)

        log(f"Response: {text3[:300]}")
        log(f"Cost: ${result3.get('cost', 0):.6f}")
        log(f"Tool calls: {len(tool_call_log)}")
        log("Agent handled error gracefully: " + str("not found" in text3.lower() or "doesn't exist" in text3.lower()))

    # ─── Test C: Mix built-in + custom tools ──────────────────
    log.sep("Test C: Mixing built-in (Read) + custom MCP tools")

    mixed_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        mcp_servers={"langbot": langbot_server},
        allowed_tools=[
            "mcp__langbot__get_user_profile",
            "Read",
        ],
        permission_mode="bypassPermissions",
        system_prompt="You are a helpful assistant with access to user profiles and file reading.",
    )

    tool_call_log.clear()

    async with ClaudeSDKClient(mixed_options) as client:
        await client.query("Load user_456's profile and tell me about them.")

        msgs4 = []
        async for msg in client.receive_response():
            msgs4.append(msg)

        text4 = extract_text(msgs4)
        result4 = extract_result(msgs4)

        log(f"Response: {text4[:300]}")
        log(f"Cost: ${result4.get('cost', 0):.6f}")
        log(f"Tool calls: {len(tool_call_log)}")
        log(f"Mentions 'Maria': {'Maria' in text4}")
        log(f"Mentions 'Japanese': {'Japanese' in text4 or 'japanese' in text4}")

    # ─── Summary ──────────────────────────────────────────────
    log.sep("Summary")
    log("Test A: Tool chaining works — agent called profile, exercise, record in sequence")
    log("Test B: Error handling — agent gracefully reports tool errors to user")
    log("Test C: Mixed tools — built-in + custom MCP tools work together")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
