"""
Experiment 06: Data Persistence Patterns for Per-User State
============================================================
Goal: Explore 3 strategies for persisting per-user data (progress, vocabulary, scores)
      across sessions: system prompt injection, custom tools as DB, hybrid.

Run: poetry run python development/code_sandbox/exp_06_data_persistence.py
Output: development/code_sandbox/output/exp_06_output.txt
"""

import asyncio
import json
import copy
from typing import Any

from shared import load_env, Log, extract_text, extract_result


# ─── Mock "database" ───────────────────────────────────────────
def fresh_user_db():
    """Return a fresh copy of mock user data (simulates DB)."""
    return {
        "user_123": {
            "name": "Alex",
            "language": "Spanish",
            "level": "A2",
            "streak": 12,
            "weak_areas": ["subjunctive mood", "irregular verbs"],
            "strong_areas": ["basic vocabulary", "present tense"],
            "vocabulary_count": 340,
            "recent_scores": [7, 8, 6, 9, 7],
            "last_session": "2026-02-19",
            "interests": ["travel", "food"],
        }
    }


# ─── Pattern A: System Prompt as Context Carrier ───────────────

async def pattern_a_system_prompt(log):
    """Inject user data into system prompt. Parse structured output to update DB."""
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock

    log.sep("Pattern A: System Prompt as Context Carrier")

    db = fresh_user_db()
    user = db["user_123"]

    system_prompt = f"""You are a Spanish language tutor for a personalized study session.

STUDENT PROFILE (loaded from database):
{json.dumps(user, indent=2)}

RULES:
- Teach based on the student's weak areas and level
- Track any new vocabulary or progress during the session
- At the END of your response, include a JSON block with updated stats:
```json_update
{{"vocabulary_learned": ["word1", "word2"], "score": 8, "topic_practiced": "topic_name", "streak_change": 1}}
```
This JSON block will be parsed to update the database."""

    # Simulate 3-turn teaching session
    turns = [
        "Let's practice! Give me a quick exercise on my weakest area.",
        "I think the answer is 'vengas'. Am I right?",
        "Great! Can you teach me a new travel-related word before we finish?",
    ]

    log(f"System prompt length: {len(system_prompt)} chars")
    log(f"Initial user state: vocab={user['vocabulary_count']}, streak={user['streak']}\n")

    total_cost = 0
    session_id = None
    all_responses = []

    for i, turn in enumerate(turns):
        log(f"--- Turn {i+1} ---")
        log(f"  Prompt: {turn}")

        opts_kwargs = dict(
            model="claude-sonnet-4-6",
            max_turns=1,
            system_prompt=system_prompt,
        )
        # Resume from previous turn
        if session_id:
            opts_kwargs["resume"] = session_id

        options = ClaudeAgentOptions(**opts_kwargs)
        response_text = ""

        async for msg in query(prompt=turn, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                total_cost = msg.total_cost_usd or 0.0
                session_id = msg.session_id

        all_responses.append(response_text)
        log(f"  Response: {response_text[:300]}...")
        log(f"  Cost so far: ${total_cost:.6f}\n")

    # Parse structured output from responses
    import re
    updates_found = 0
    for resp in all_responses:
        match = re.search(r'```json_update\s*\n(.*?)\n```', resp, re.DOTALL)
        if match:
            updates_found += 1
            try:
                update = json.loads(match.group(1))
                log(f"  Parsed update: {json.dumps(update)}")
                # Apply to mock DB
                if "vocabulary_learned" in update:
                    user["vocabulary_count"] += len(update["vocabulary_learned"])
                if "score" in update:
                    user["recent_scores"].append(update["score"])
                if "streak_change" in update:
                    user["streak"] += update["streak_change"]
            except json.JSONDecodeError as e:
                log(f"  Failed to parse update: {e}")

    log(f"\nPattern A Results:")
    log(f"  Updates parsed from responses: {updates_found}")
    log(f"  Updated user state: vocab={user['vocabulary_count']}, streak={user['streak']}")
    log(f"  Recent scores: {user['recent_scores']}")
    log(f"  Total cost (3 turns): ${total_cost:.6f}")
    log(f"  Reliability: {'Good' if updates_found >= 2 else 'Poor'} ({updates_found}/3 turns had parseable updates)")

    return {"pattern": "A", "cost": total_cost, "updates_found": updates_found, "turns": 3}


# ─── Pattern B: Custom Tools as DB Interface ──────────────────

async def pattern_b_tools(log):
    """Agent uses custom tools to read/write user data during conversation."""
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    log.sep("Pattern B: Custom Tools as DB Interface")

    db = fresh_user_db()
    tool_calls_log = []

    @tool("read_user_data", "Read a specific field from user's learning profile", {"user_id": str, "field": str})
    async def read_user_data(args: dict[str, Any]) -> dict[str, Any]:
        tool_calls_log.append({"op": "read", "field": args["field"]})
        user_id = args["user_id"]
        field = args["field"]
        if user_id in db:
            if field == "all":
                return {"content": [{"type": "text", "text": json.dumps(db[user_id], indent=2)}]}
            elif field in db[user_id]:
                return {"content": [{"type": "text", "text": json.dumps({field: db[user_id][field]})}]}
            return {"content": [{"type": "text", "text": f"Field '{field}' not found"}], "is_error": True}
        return {"content": [{"type": "text", "text": f"User {user_id} not found"}], "is_error": True}

    @tool("write_user_data", "Update a field in user's learning profile", {"user_id": str, "field": str, "value": str})
    async def write_user_data(args: dict[str, Any]) -> dict[str, Any]:
        tool_calls_log.append({"op": "write", "field": args["field"], "value": args["value"]})
        user_id = args["user_id"]
        field = args["field"]
        value = args["value"]
        if user_id in db:
            # Parse value as JSON if possible
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                parsed = value
            db[user_id][field] = parsed
            return {"content": [{"type": "text", "text": json.dumps({"status": "updated", "field": field, "new_value": parsed})}]}
        return {"content": [{"type": "text", "text": f"User {user_id} not found"}], "is_error": True}

    server = create_sdk_mcp_server(
        name="userdb",
        version="1.0.0",
        tools=[read_user_data, write_user_data],
    )

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=10,
        mcp_servers={"userdb": server},
        allowed_tools=["mcp__userdb__read_user_data", "mcp__userdb__write_user_data"],
        system_prompt=(
            "You are a Spanish language tutor. You have access to a user database via tools. "
            "ALWAYS use read_user_data to load the student's profile before teaching. "
            "ALWAYS use write_user_data to record progress after exercises (update vocabulary_count, "
            "recent_scores, streak, etc). User ID is 'user_123'."
        ),
    )

    turns = [
        "Hi! Let's start a study session. Load my profile and give me a quick exercise.",
        "I think the answer is 'vengas'. Record my result!",
        "Teach me a new travel word and update my vocabulary count.",
    ]

    log(f"Initial DB state: vocab={db['user_123']['vocabulary_count']}, streak={db['user_123']['streak']}\n")

    total_cost = 0.0

    async with ClaudeSDKClient(options) as client:
        for i, turn in enumerate(turns):
            log(f"--- Turn {i+1} ---")
            log(f"  Prompt: {turn}")
            tool_calls_log.clear()

            await client.query(turn)

            response_text = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            log(f"  [Tool] {block.name}({json.dumps(block.input)[:120]})")
                        elif isinstance(block, TextBlock):
                            response_text += block.text
                elif isinstance(msg, ResultMessage):
                    total_cost = msg.total_cost_usd or 0.0

            log(f"  Response: {response_text[:250]}...")
            log(f"  Tool calls this turn: {len(tool_calls_log)}")
            reads = sum(1 for tc in tool_calls_log if tc["op"] == "read")
            writes = sum(1 for tc in tool_calls_log if tc["op"] == "write")
            log(f"    reads={reads}, writes={writes}")
            log(f"  Cost so far: ${total_cost:.6f}\n")

    log(f"Pattern B Results:")
    log(f"  Final DB state: {json.dumps(db['user_123'], indent=2)}")
    log(f"  Total cost (3 turns): ${total_cost:.6f}")
    write_count = sum(1 for tc in tool_calls_log if tc["op"] == "write")
    log(f"  Agent auto-updated DB: {write_count > 0}")

    return {"pattern": "B", "cost": total_cost, "db_state": copy.deepcopy(db["user_123"]), "turns": 3}


# ─── Pattern C: Hybrid (System Prompt + Tools for Updates) ────

async def pattern_c_hybrid(log):
    """System prompt carries profile snapshot; tools handle live updates."""
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    log.sep("Pattern C: Hybrid (System Prompt Snapshot + Tools for Updates)")

    db = fresh_user_db()
    tool_calls_log = []

    @tool("update_progress", "Update student's progress after an exercise or activity", {
        "user_id": str, "field": str, "value": str
    })
    async def update_progress(args: dict[str, Any]) -> dict[str, Any]:
        tool_calls_log.append({"field": args["field"], "value": args["value"]})
        user_id = args["user_id"]
        if user_id in db:
            try:
                parsed = json.loads(args["value"])
            except (json.JSONDecodeError, TypeError):
                parsed = args["value"]
            db[user_id][args["field"]] = parsed
            return {"content": [{"type": "text", "text": json.dumps({
                "status": "updated", "field": args["field"], "new_value": parsed
            })}]}
        return {"content": [{"type": "text", "text": f"User {user_id} not found"}], "is_error": True}

    server = create_sdk_mcp_server(
        name="progress",
        version="1.0.0",
        tools=[update_progress],
    )

    user_snapshot = json.dumps(db["user_123"], indent=2)

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=10,
        mcp_servers={"progress": server},
        allowed_tools=["mcp__progress__update_progress"],
        system_prompt=f"""You are a Spanish language tutor.

STUDENT PROFILE (snapshot from database):
{user_snapshot}

You already have the student's profile — no need to load it. Use the update_progress tool
to save any changes (vocabulary_count, recent_scores, streak, etc.) after exercises.
User ID is 'user_123'.""",
    )

    turns = [
        "Let's practice! Give me a quick exercise on my weakest area.",
        "I think the answer is 'vengas'. Record my result!",
        "Teach me a new travel word and update my vocabulary count.",
    ]

    log(f"System prompt includes profile snapshot: {len(user_snapshot)} chars")
    log(f"Initial DB state: vocab={db['user_123']['vocabulary_count']}, streak={db['user_123']['streak']}\n")

    total_cost = 0.0

    async with ClaudeSDKClient(options) as client:
        for i, turn in enumerate(turns):
            log(f"--- Turn {i+1} ---")
            log(f"  Prompt: {turn}")
            turn_tool_calls = 0

            await client.query(turn)

            response_text = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            turn_tool_calls += 1
                            log(f"  [Tool] {block.name}({json.dumps(block.input)[:120]})")
                        elif isinstance(block, TextBlock):
                            response_text += block.text
                elif isinstance(msg, ResultMessage):
                    total_cost = msg.total_cost_usd or 0.0

            log(f"  Response: {response_text[:250]}...")
            log(f"  Tool calls: {turn_tool_calls}")
            log(f"  Cost so far: ${total_cost:.6f}\n")

    log(f"Pattern C Results:")
    log(f"  Final DB state: {json.dumps(db['user_123'], indent=2)}")
    log(f"  Total cost (3 turns): ${total_cost:.6f}")
    log(f"  Total tool calls (writes): {len(tool_calls_log)}")

    return {"pattern": "C", "cost": total_cost, "db_state": copy.deepcopy(db["user_123"]),
            "tool_writes": len(tool_calls_log), "turns": 3}


async def main():
    load_env()
    log = Log("exp_06_output")

    log.sep("Experiment 06: Data Persistence Patterns")

    result_a = await pattern_a_system_prompt(log)
    result_b = await pattern_b_tools(log)
    result_c = await pattern_c_hybrid(log)

    # ─── Comparison ────────────────────────────────────────────
    log.sep("Pattern Comparison")

    log(f"{'Pattern':<30} {'Cost':<12} {'Turns':<8} {'Notes'}")
    log("-" * 80)
    log(f"{'A: System Prompt':<30} ${result_a['cost']:<11.6f} {result_a['turns']:<8} Updates parsed: {result_a['updates_found']}/3")
    log(f"{'B: Tools as DB':<30} ${result_b['cost']:<11.6f} {result_b['turns']:<8} DB updated via tools")
    log(f"{'C: Hybrid':<30} ${result_c['cost']:<11.6f} {result_c['turns']:<8} Writes: {result_c['tool_writes']}")

    log.sep("Analysis")
    log("Pattern A (System Prompt):")
    log("  + Cheapest (no tool call overhead)")
    log("  + Simple implementation")
    log("  - Requires parsing structured output (fragile)")
    log("  - Static snapshot — can't update mid-session reliably")
    log("")
    log("Pattern B (Tools as DB):")
    log("  + Agent decides when to read/write (dynamic)")
    log("  + Data persists immediately per tool call")
    log("  - More tool calls = higher cost")
    log("  - Agent may not always call write_user_data")
    log("")
    log("Pattern C (Hybrid):")
    log("  + Profile available immediately (no read tool calls)")
    log("  + Writes happen via tools (reliable persistence)")
    log("  + Balance of cost and reliability")
    log("  - Slightly more complex setup")
    log("")
    log("Recommendation for bot:")
    log("  Use Pattern C (Hybrid) — system prompt carries snapshot,")
    log("  tools handle updates. Best cost/reliability trade-off.")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
