"""
Experiment 14: Smooth Conversation Continuity
==============================================
Goal: Test patterns for making conversations feel seamless:
      - Time-aware greetings (detect 10h+ gaps)
      - Progress acknowledgment on return
      - Continuation from where user stopped
      - Smooth transitions between session types

Run: poetry run python development/code_sandbox/exp_14_smooth_conversations.py
Output: development/code_sandbox/output/exp_14_output.txt
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any

from shared import load_env, Log


# ─── Simulated user with rich history ─────────────────────────

def make_user(last_session_hours_ago: float, last_activity: dict | None = None):
    """Create a user profile with a specific time gap."""
    now = datetime.now()
    last = now - timedelta(hours=last_session_hours_ago)
    return {
        "name": "Alex",
        "language": "Spanish",
        "level": "A2",
        "streak": 12,
        "weak_areas": ["subjunctive mood", "irregular verbs"],
        "strong_areas": ["basic vocabulary", "present tense", "greetings"],
        "interests": ["travel", "food"],
        "vocabulary_count": 340,
        "recent_scores": [7, 8, 6, 9, 7],
        "preferred_difficulty": "normal",
        "last_session": last.isoformat(),
        "last_activity": last_activity or {
            "type": "exercise",
            "topic": "irregular verbs",
            "status": "completed",
            "score": 7,
            "last_exercise": "Fill the blank: Ella ___ (poder) hacerlo. → pudo",
            "words_practiced": ["pudo", "quiso", "vino"],
            "session_summary": "Practiced irregular preterite verbs. Scored 7/10. "
                               "Struggled with 'querer→quiso' and 'venir→vino'.",
        },
        "milestones": {
            "vocabulary_count": 340,
            "days_streak": 12,
            "exercises_completed": 45,
            "last_level_up": "A1→A2 on 2026-02-10",
        },
    }


# ─── Session context builder ─────────────────────────────────

def compute_session_context(user: dict) -> dict:
    """Compute dynamic session context from user profile + current time."""
    now = datetime.now()
    last_str = user.get("last_session")
    if last_str:
        last_dt = datetime.fromisoformat(last_str)
        gap_hours = (now - last_dt).total_seconds() / 3600
    else:
        gap_hours = 999  # new user

    # Determine greeting style based on gap
    if gap_hours < 1:
        greeting_style = "continuation"  # "Welcome back! Let's keep going."
        greeting_note = "User returned within the same hour. Treat as continuation, no greeting needed."
    elif gap_hours < 4:
        greeting_style = "short_break"
        greeting_note = "Short break. Brief acknowledgment, then dive in."
    elif gap_hours < 10:
        greeting_style = "normal_return"
        greeting_note = "Normal return. Quick hello, mention what they did last time."
    elif gap_hours < 24:
        greeting_style = "long_break"
        greeting_note = (
            "Been away 10+ hours. Warm greeting, acknowledge their streak, "
            "summarize last session progress, suggest what to do today."
        )
    elif gap_hours < 72:
        greeting_style = "day_plus_break"
        greeting_note = (
            "Away for 1-3 days. Enthusiastic welcome back, celebrate streak if intact, "
            "motivate them, offer easy warm-up to get back in."
        )
    else:
        greeting_style = "long_absence"
        greeting_note = (
            "Away for 3+ days. Very warm welcome, no guilt. Mention they can pick up "
            "where they left off. Suggest a review session to refresh memory."
        )

    # Check for milestones to celebrate
    celebrations = []
    m = user.get("milestones", {})
    if m.get("days_streak", 0) % 10 == 0 and m.get("days_streak", 0) > 0:
        celebrations.append(f"streak milestone: {m['days_streak']} days!")
    if m.get("vocabulary_count", 0) % 100 == 0 and m.get("vocabulary_count", 0) > 0:
        celebrations.append(f"vocabulary milestone: {m['vocabulary_count']} words!")
    if m.get("exercises_completed", 0) % 50 == 0 and m.get("exercises_completed", 0) > 0:
        celebrations.append(f"exercise milestone: {m['exercises_completed']} completed!")

    return {
        "gap_hours": round(gap_hours, 1),
        "greeting_style": greeting_style,
        "greeting_note": greeting_note,
        "celebrations": celebrations,
        "time_of_day": "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening",
        "day_of_week": now.strftime("%A"),
    }


def build_system_prompt(user: dict, session_ctx: dict) -> str:
    """Build a system prompt with dynamic session context."""
    last_activity = user.get("last_activity", {})

    return f"""You are a personalized Spanish language tutor for {user['name']}.

## STUDENT PROFILE
Name: {user['name']}
Level: {user['level']}
Streak: {user['streak']} days
Weak areas: {', '.join(user['weak_areas'])}
Strong areas: {', '.join(user['strong_areas'])}
Interests: {', '.join(user['interests'])}
Vocabulary: {user['vocabulary_count']} words
Recent scores: {user['recent_scores'][-5:]}
Preferred difficulty: {user.get('preferred_difficulty', 'normal')}

## LAST SESSION
Summary: {last_activity.get('session_summary', 'No previous session')}
Last exercise: {last_activity.get('last_exercise', 'None')}
Score: {last_activity.get('score', 'N/A')}/10
Words practiced: {', '.join(last_activity.get('words_practiced', []))}
Status: {last_activity.get('status', 'unknown')}

## SESSION CONTEXT
Time gap since last session: {session_ctx['gap_hours']} hours
Time of day: {session_ctx['time_of_day']}
Day: {session_ctx['day_of_week']}
Greeting style: {session_ctx['greeting_style']}

## CONVERSATION STYLE INSTRUCTIONS
{session_ctx['greeting_note']}
{('Celebrate: ' + ', '.join(session_ctx['celebrations'])) if session_ctx['celebrations'] else ''}

RULES:
- Match your greeting warmth to the time gap (see greeting style above)
- Always acknowledge what they did last time (see LAST SESSION)
- If they want to continue from where they stopped, seamlessly pick up the same topic
- If they want something new, transition naturally ("Great work on verbs last time! Ready for something fresh?")
- Reference specific words/scores from their history to show you remember
- Never say "I don't have access to your history" — you DO have it above
- Keep responses concise (3-5 sentences for greetings, then get to learning)
"""


# ─── Tests ────────────────────────────────────────────────────

async def run_test(log, label: str, user: dict, user_message: str):
    """Run a single conversation test and return results."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
    )

    session_ctx = compute_session_context(user)
    system_prompt = build_system_prompt(user, session_ctx)

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=system_prompt,
    )

    response_text = ""
    cost = 0.0
    t0 = time.monotonic()

    async for msg in query(prompt=user_message, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0

    wall_ms = int((time.monotonic() - t0) * 1000)

    log(f"  Greeting style: {session_ctx['greeting_style']} ({session_ctx['gap_hours']}h gap)")
    log(f"  User: \"{user_message}\"")
    log(f"  Bot: {response_text[:500]}")
    log(f"  Cost: ${cost:.6f} | Wall: {wall_ms}ms")

    # Quality checks
    checks = {
        "mentions_name": user["name"].lower() in response_text.lower(),
        "references_last_session": any(w in response_text.lower() for w in [
            "last time", "earlier", "before", "previous", "you were", "you did",
            "irregular", "pudo", "quiso", "vino", "preterite",
        ]),
        "mentions_score_or_progress": any(w in response_text.lower() for w in [
            "7/10", "7 out of", "score", "streak", "12 day", "12-day", "progress",
        ]),
        "offers_continuation": any(w in response_text.lower() for w in [
            "continue", "pick up", "keep going", "where we left", "same topic",
            "more practice", "again", "review",
        ]),
        "warm_not_robotic": not any(w in response_text.lower() for w in [
            "i don't have access", "i cannot recall", "as an ai",
        ]),
    }
    checks["score"] = sum(checks.values())

    log(f"  Quality: {checks['score']}/5 {json.dumps({k: v for k, v in checks.items() if k != 'score'})}\n")

    return {
        "label": label,
        "greeting_style": session_ctx["greeting_style"],
        "gap_hours": session_ctx["gap_hours"],
        "response": response_text,
        "cost": cost,
        "wall_ms": wall_ms,
        "checks": checks,
    }


async def test_time_gaps(log):
    """Test greeting behavior across different time gaps."""
    log.sep("Test A: Time-Aware Greetings")

    gaps = [
        (0.3, "continuation", "Hi"),
        (2.0, "short_break", "Hey, I'm back"),
        (6.0, "normal_return", "Hi!"),
        (14.0, "long_break", "Hello!"),
        (36.0, "day_plus_break", "Hey! I'm back"),
        (96.0, "long_absence", "Hi, it's been a while"),
    ]

    results = []
    for hours, expected_style, user_msg in gaps:
        log(f"--- {expected_style} ({hours}h gap) ---")
        user = make_user(last_session_hours_ago=hours)
        result = await run_test(log, f"gap_{hours}h", user, user_msg)
        results.append(result)

    return results


async def test_continuation(log):
    """Test picking up from where user stopped."""
    log.sep("Test B: Continuation from Last Session")

    # Scenario 1: User explicitly asks to continue
    log("--- Explicit continuation ---")
    user = make_user(
        last_session_hours_ago=14.0,
        last_activity={
            "type": "exercise",
            "topic": "irregular verbs",
            "status": "incomplete",
            "score": None,
            "last_exercise": "Fill the blank: Ella ___ (poder) hacerlo.",
            "words_practiced": ["pudo", "quiso"],
            "session_summary": "Started irregular preterite verbs exercise but didn't finish. "
                               "Got 5 out of 8 questions done. Last question was about 'poder'.",
        },
    )
    r1 = await run_test(log, "explicit_continue", user,
                         "Can we continue from where I stopped last time?")

    # Scenario 2: User sends a generic greeting — bot should offer to continue
    log("--- Generic greeting with incomplete task ---")
    r2 = await run_test(log, "generic_with_incomplete", user, "Hey!")

    # Scenario 3: User wants something different
    log("--- User wants new topic despite incomplete ---")
    r3 = await run_test(log, "new_topic_request", user,
                         "I want to learn some cooking vocabulary today")

    return [r1, r2, r3]


async def test_progress_acknowledgment(log):
    """Test how bot acknowledges milestones and progress."""
    log.sep("Test C: Progress & Milestone Acknowledgment")

    # User just hit a streak milestone
    log("--- Streak milestone (day 20) ---")
    user = make_user(last_session_hours_ago=14.0)
    user["streak"] = 20
    user["milestones"]["days_streak"] = 20
    r1 = await run_test(log, "streak_milestone", user, "Good morning!")

    # User just crossed vocabulary milestone
    log("--- Vocabulary milestone (400 words) ---")
    user2 = make_user(last_session_hours_ago=14.0)
    user2["vocabulary_count"] = 400
    user2["milestones"]["vocabulary_count"] = 400
    r2 = await run_test(log, "vocab_milestone", user2, "Hi!")

    # User improved after struggling
    log("--- Score improvement ---")
    user3 = make_user(last_session_hours_ago=6.0)
    user3["recent_scores"] = [4, 5, 4, 6, 9]
    user3["last_activity"]["score"] = 9
    user3["last_activity"]["session_summary"] = (
        "Big improvement! Scored 9/10 on irregular verbs after struggling at 4-5 range. "
        "Finally mastered 'querer→quiso'."
    )
    r3 = await run_test(log, "score_improvement", user3,
                         "Hey! That last session was really good right?")

    return [r1, r2, r3]


async def test_proactive_to_interactive(log):
    """Test smooth transition from proactive notification to interactive chat."""
    from claude_agent_sdk import (
        tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    log.sep("Test D: Proactive → Interactive Transition")

    user = make_user(last_session_hours_ago=14.0)
    user["pending_reviews"] = ["pudo", "quiso", "vino"]

    @tool("get_pending_reviews", "Get words due for review", {"user_id": str})
    async def get_pending_reviews(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps({
            "pending": user["pending_reviews"],
            "count": len(user["pending_reviews"]),
        })}]}

    @tool("send_notification", "Send a message to the user", {"user_id": str, "message": str})
    async def send_notification(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps({
            "status": "sent", "message": args["message"]
        })}]}

    server = create_sdk_mcp_server(name="bot", version="1.0.0",
                                    tools=[get_pending_reviews, send_notification])

    # Phase 1: Proactive notification (cron)
    log("--- Phase 1: Proactive notification ---")

    session_ctx = compute_session_context(user)
    proactive_prompt = build_system_prompt(user, session_ctx)
    proactive_prompt += """
## PROACTIVE TASK
This is a cron-triggered session. Send a brief, warm notification to the user.
Include what they should review and reference their last session."""

    proactive_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        thinking={"type": "disabled"},
        mcp_servers={"bot": server},
        allowed_tools=["mcp__bot__get_pending_reviews", "mcp__bot__send_notification"],
        system_prompt=proactive_prompt,
    )

    notification_text = ""
    proactive_session_id = None

    async with ClaudeSDKClient(proactive_options) as client:
        await client.query("Run the proactive review notification for user_123.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        notification_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        log(f"  [Tool] {block.name}")
            elif isinstance(msg, ResultMessage):
                proactive_session_id = msg.session_id
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"  Notification: {notification_text[:300]}...")

    # Phase 2: User responds — new session with context about the notification
    log("\n--- Phase 2: User responds to notification ---")

    # In production: store notification text in DB, load it as context
    interactive_prompt = build_system_prompt(user, session_ctx)
    interactive_prompt += f"""
## CONTEXT: USER IS RESPONDING TO A NOTIFICATION
You just sent this notification to the user:
\"\"\"{notification_text[:300]}\"\"\"

The user is now responding. Continue naturally from the notification context.
Don't repeat the notification — they already read it. Pick up from there."""

    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions as Opts
    from claude_agent_sdk import AssistantMessage as AM, ResultMessage as RM, TextBlock as TB

    interactive_options = Opts(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=interactive_prompt,
    )

    response_text = ""
    async for msg in sdk_query(prompt="Hey! I saw your message. Let's review those words!", options=interactive_options):
        if isinstance(msg, AM):
            for block in msg.content:
                if isinstance(block, TB):
                    response_text += block.text
        elif isinstance(msg, RM):
            log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"  User: \"Hey! I saw your message. Let's review those words!\"")
    log(f"  Bot: {response_text[:400]}...")

    # Check quality
    doesnt_repeat = "notification" not in response_text.lower()[:100]
    references_words = any(w in response_text.lower() for w in ["pudo", "quiso", "vino"])
    smooth = not any(w in response_text.lower() for w in ["i don't know what", "what notification"])

    log(f"  Doesn't repeat notification: {doesnt_repeat}")
    log(f"  References pending words: {references_words}")
    log(f"  Smooth transition: {smooth}")


async def test_multi_turn_continuity(log):
    """Test that mid-session context is maintained across turns."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions,
        AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Test E: Multi-Turn Mid-Session Continuity")

    user = make_user(last_session_hours_ago=0.5)  # just came back
    session_ctx = compute_session_context(user)
    prompt = build_system_prompt(user, session_ctx)

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=2,
        thinking={"type": "disabled"},
        system_prompt=prompt,
    )

    turns = [
        "Teach me 3 food words for a restaurant situation",
        "What was the second word you just taught me? Quiz me on it",
        "La cuenta. Am I right?",
    ]

    log("Simulating 3-turn conversation to test mid-session memory:\n")

    async with ClaudeSDKClient(options) as client:
        for i, turn in enumerate(turns):
            log(f"  Turn {i+1} User: \"{turn}\"")
            await client.query(turn)

            response_text = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_text += block.text
                elif isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0.0

            log(f"  Turn {i+1} Bot: {response_text[:250]}...")
            log(f"  Cost: ${cost:.6f}\n")


async def main():
    load_env()
    log = Log("exp_14_output")

    log.sep("Experiment 14: Smooth Conversation Continuity")

    gap_results = await test_time_gaps(log)
    cont_results = await test_continuation(log)
    prog_results = await test_progress_acknowledgment(log)
    await test_proactive_to_interactive(log)
    await test_multi_turn_continuity(log)

    # ─── Summary ──────────────────────────────────────────────
    log.sep("Summary: Time-Gap Greeting Quality")
    log(f"{'Gap':<12} {'Style':<18} {'Quality':<10} {'Cost':<12}")
    log("-" * 52)
    for r in gap_results:
        log(f"{r['gap_hours']:<12} {r['greeting_style']:<18} {r['checks']['score']}/5{'':<5} ${r['cost']:<11.6f}")

    log.sep("Summary: Continuation Quality")
    for r in cont_results:
        log(f"  {r['label']}: quality={r['checks']['score']}/5, "
            f"refs_last={r['checks']['references_last_session']}, "
            f"offers_continue={r['checks']['offers_continuation']}")

    log.sep("Summary: Progress Acknowledgment")
    for r in prog_results:
        log(f"  {r['label']}: quality={r['checks']['score']}/5, "
            f"refs_progress={r['checks']['mentions_score_or_progress']}")

    log.sep("Key Findings")
    log("See output above for detailed results per test.")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
