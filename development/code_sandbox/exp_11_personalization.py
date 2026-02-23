"""
Experiment 11: Personalization Approaches Comparison
=====================================================
Goal: Compare 4 personalization approaches side-by-side with 2 mock users.
      Measure output quality, cost, and developer ergonomics.

Run: poetry run python development/code_sandbox/exp_11_personalization.py
Output: development/code_sandbox/output/exp_11_output.txt
"""

import asyncio
import json
import time
from typing import Any

from shared import load_env, Log


# ─── Mock user data ───────────────────────────────────────────
USERS = {
    "user_A": {
        "name": "Yuki",
        "language": "Japanese",
        "level": "A1",
        "streak": 3,
        "weak_areas": ["katakana", "particles"],
        "strong_areas": ["hiragana"],
        "vocabulary_count": 80,
        "recent_scores": [6, 5, 7],
        "interests": ["anime", "cooking"],
        "last_session": "2026-02-19",
        "pending_reviews": ["konnichiwa", "arigatou", "sumimasen"],
    },
    "user_B": {
        "name": "Pierre",
        "language": "French",
        "level": "B1",
        "streak": 45,
        "weak_areas": ["subjunctive mood", "passé composé vs imparfait"],
        "strong_areas": ["present tense", "vocabulary", "articles"],
        "vocabulary_count": 1200,
        "recent_scores": [8, 9, 7, 8, 9],
        "interests": ["cinema", "philosophy"],
        "last_session": "2026-02-20",
        "pending_reviews": ["néanmoins", "davantage", "auparavant"],
    },
}

TASK_PROMPT = "Create a personalized 5-minute warm-up activity for today's session. Be specific and concise."


def quality_check(response: str, user: dict) -> dict:
    """Simple heuristic quality checks for personalization."""
    checks = {
        "mentions_name": user["name"].lower() in response.lower(),
        "mentions_language": user["language"].lower() in response.lower(),
        "mentions_weak_area": any(w.lower() in response.lower() for w in user["weak_areas"]),
        "mentions_interest": any(i.lower() in response.lower() for i in user["interests"]),
        "mentions_level": user["level"] in response,
        "has_exercise": any(w in response.lower() for w in ["exercise", "activity", "practice", "quiz", "translate", "fill"]),
    }
    checks["score"] = sum(checks.values())
    return checks


# ─── Approach A: System Prompt Injection ──────────────────────

async def approach_a(log, user_id: str):
    """Simple system prompt injection — no tools needed."""
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock

    user = USERS[user_id]

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=f"""You are a personalized {user['language']} language tutor.

STUDENT PROFILE:
{json.dumps(user, indent=2)}

Tailor every activity to this student's level, weak areas, and interests.""",
    )

    response_text = ""
    cost = 0.0
    t0 = time.monotonic()

    async for msg in query(prompt=TASK_PROMPT, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0

    wall_ms = int((time.monotonic() - t0) * 1000)
    quality = quality_check(response_text, user)

    return {
        "approach": "A: System Prompt",
        "user": user_id,
        "cost": cost,
        "wall_ms": wall_ms,
        "quality": quality,
        "response_preview": response_text[:300],
    }


# ─── Approach B: Tool-Based Context Loading ──────────────────

async def approach_b(log, user_id: str):
    """Agent decides what to load via tools."""
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    @tool("get_user_profile", "Get a student's full learning profile", {"user_id": str})
    async def get_user_profile(args: dict[str, Any]) -> dict[str, Any]:
        uid = args["user_id"]
        if uid in USERS:
            return {"content": [{"type": "text", "text": json.dumps(USERS[uid], indent=2)}]}
        return {"content": [{"type": "text", "text": f"User {uid} not found"}], "is_error": True}

    @tool("get_pending_reviews", "Get words due for spaced repetition review", {"user_id": str})
    async def get_pending_reviews(args: dict[str, Any]) -> dict[str, Any]:
        uid = args["user_id"]
        if uid in USERS:
            reviews = USERS[uid].get("pending_reviews", [])
            return {"content": [{"type": "text", "text": json.dumps({"pending": reviews, "count": len(reviews)})}]}
        return {"content": [{"type": "text", "text": f"User {uid} not found"}], "is_error": True}

    server = create_sdk_mcp_server(name="learner", version="1.0.0", tools=[get_user_profile, get_pending_reviews])

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        thinking={"type": "disabled"},
        mcp_servers={"learner": server},
        allowed_tools=["mcp__learner__get_user_profile", "mcp__learner__get_pending_reviews"],
        system_prompt=f"You are a language tutor. Use tools to load student data for {user_id}. Be concise.",
    )

    response_text = ""
    cost = 0.0
    tool_calls = 0
    t0 = time.monotonic()

    async with ClaudeSDKClient(options) as client:
        await client.query(TASK_PROMPT)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls += 1
                    elif isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    wall_ms = int((time.monotonic() - t0) * 1000)
    quality = quality_check(response_text, USERS[user_id])

    return {
        "approach": "B: Tool-Based",
        "user": user_id,
        "cost": cost,
        "wall_ms": wall_ms,
        "quality": quality,
        "tool_calls": tool_calls,
        "response_preview": response_text[:300],
    }


# ─── Approach C: Session Resume + Tools ──────────────────────

async def approach_c(log, user_id: str):
    """First run establishes context; second run resumes with session memory."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock,
    )

    user = USERS[user_id]

    # Phase 1: establish context session
    options1 = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=f"You are a {user['language']} tutor. Be very brief.",
    )

    session_id = None
    phase1_cost = 0.0
    async for msg in query(
        prompt=f"I'm {user['name']}, learning {user['language']} at {user['level']}. "
               f"My weak areas: {', '.join(user['weak_areas'])}. "
               f"I'm interested in {', '.join(user['interests'])}. "
               f"My pending reviews: {', '.join(user.get('pending_reviews', []))}. Just acknowledge.",
        options=options1,
    ):
        if isinstance(msg, ResultMessage):
            session_id = msg.session_id
            phase1_cost = msg.total_cost_usd or 0.0

    # Phase 2: resume session (no tools — relies on session memory)
    options2 = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        resume=session_id,
    )

    response_text = ""
    cost = 0.0
    t0 = time.monotonic()

    async for msg in query(prompt=TASK_PROMPT, options=options2):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0

    wall_ms = int((time.monotonic() - t0) * 1000)
    quality = quality_check(response_text, user)

    return {
        "approach": "C: Session Resume",
        "user": user_id,
        "cost": cost,
        "phase1_cost": phase1_cost,
        "wall_ms": wall_ms,
        "quality": quality,
        "response_preview": response_text[:300],
    }


# ─── Approach D: Hybrid (Snapshot + Tools for Live Data) ─────

async def approach_d(log, user_id: str):
    """System prompt carries snapshot; tools for live data only."""
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    user = USERS[user_id]

    @tool("get_pending_reviews", "Get words due for spaced repetition review today", {"user_id": str})
    async def get_pending_reviews(args: dict[str, Any]) -> dict[str, Any]:
        uid = args["user_id"]
        if uid in USERS:
            return {"content": [{"type": "text", "text": json.dumps({
                "pending": USERS[uid].get("pending_reviews", []),
                "count": len(USERS[uid].get("pending_reviews", [])),
            })}]}
        return {"content": [{"type": "text", "text": "User not found"}], "is_error": True}

    server = create_sdk_mcp_server(name="live", version="1.0.0", tools=[get_pending_reviews])

    # System prompt has static profile; tool provides live data
    profile_snapshot = {k: v for k, v in user.items() if k != "pending_reviews"}

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        thinking={"type": "disabled"},
        mcp_servers={"live": server},
        allowed_tools=["mcp__live__get_pending_reviews"],
        system_prompt=f"""You are a personalized {user['language']} language tutor.

STUDENT PROFILE (snapshot):
{json.dumps(profile_snapshot, indent=2)}

Use the get_pending_reviews tool to load today's review items. User ID: {user_id}.""",
    )

    response_text = ""
    cost = 0.0
    tool_calls = 0
    t0 = time.monotonic()

    async with ClaudeSDKClient(options) as client:
        await client.query(TASK_PROMPT)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls += 1
                    elif isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    wall_ms = int((time.monotonic() - t0) * 1000)
    quality = quality_check(response_text, user)

    return {
        "approach": "D: Hybrid",
        "user": user_id,
        "cost": cost,
        "wall_ms": wall_ms,
        "quality": quality,
        "tool_calls": tool_calls,
        "response_preview": response_text[:300],
    }


async def main():
    load_env()
    log = Log("exp_11_output")

    log.sep("Experiment 11: Personalization Approaches Comparison")
    log(f"Users: {list(USERS.keys())}")
    log(f"Task: {TASK_PROMPT}\n")

    all_results = []

    for user_id in ["user_A", "user_B"]:
        user = USERS[user_id]
        log.sep(f"User: {user['name']} ({user['language']} {user['level']})")

        for approach_fn, label in [
            (approach_a, "A"),
            (approach_b, "B"),
            (approach_c, "C"),
            (approach_d, "D"),
        ]:
            log(f"\n--- Approach {label} ---")
            result = await approach_fn(log, user_id)
            all_results.append(result)
            log(f"  Cost: ${result['cost']:.6f}")
            log(f"  Wall: {result['wall_ms']}ms")
            log(f"  Quality: {result['quality']['score']}/6")
            log(f"  Checks: {json.dumps({k: v for k, v in result['quality'].items() if k != 'score'})}")
            log(f"  Response: {result['response_preview'][:200]}...")

    # ─── Final Comparison ─────────────────────────────────────
    log.sep("Final Comparison Table")

    log(f"{'Approach':<28} {'User':<10} {'Cost':<12} {'Quality':<10} {'Wall ms':<10}")
    log("-" * 70)
    for r in all_results:
        user_label = USERS[r["user"]]["name"]
        log(f"{r['approach']:<28} {user_label:<10} ${r['cost']:<11.6f} {r['quality']['score']}/6{'':<5} {r['wall_ms']:<10}")

    # Averages by approach
    log.sep("Average by Approach")
    approaches = ["A: System Prompt", "B: Tool-Based", "C: Session Resume", "D: Hybrid"]
    for approach_name in approaches:
        results = [r for r in all_results if r["approach"] == approach_name]
        if results:
            avg_cost = sum(r["cost"] for r in results) / len(results)
            avg_quality = sum(r["quality"]["score"] for r in results) / len(results)
            avg_wall = sum(r["wall_ms"] for r in results) / len(results)
            log(f"  {approach_name:<28} avg_cost=${avg_cost:.6f}  avg_quality={avg_quality:.1f}/6  avg_wall={avg_wall:.0f}ms")

    log.sep("Recommendations")
    log("For scheduled/proactive tasks (cron): Approach A (cheapest, good enough quality)")
    log("For interactive chat sessions: Approach D (hybrid — rich context + live data)")
    log("For first-time users: Approach B (tool-based — flexible, agent-driven)")
    log("Avoid Approach C for high-volume: resume cost grows with conversation history")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
